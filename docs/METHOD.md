# 方法：把占座写成可计算的因果

本文档描述 `seatlab.engine` 的完整算法。所有公式都对应代码位置，任何一条都可以单独替换做敏感性分析。

---

## 1. 为什么"座位 = 服务台"成立

占座问题的本质不是"谁先来"，而是**服务台被占用却不提供服务**。经典的排队论假设服务台只要被占用就在工作；图书馆里存在第三种状态：座位被占、人不在、也不提供服务。于是把经典模型的状态机从两种扩成四种：

| 状态 | 含义 | 计入 |
|---|---|---|
| `FREE` | 空闲，可分配 | 可用容量 |
| `PRESENT` | 有人在场学习 | 有效服务时间 |
| `HELD` | 有人占着但离开（占座） | **空占时长（外部性）** |
| `BOOKED` | 预约锁定，尚未到场 | 预留容量 |

`empty_share = held / (present + held)` 就是"被占用的座位时间里，有多大比例其实没人在学"。这个比值不依赖容量、不依赖需求，是同一间图书馆跨情景可比的指标，也是全项目的核心因变量。

**一天**：08:00 起 840 分钟，逐分钟离散推进（`engine.simulate_day`）。每一步先做座位计数快照，再处理该分钟的预约、到达、离座、返回、检查、离馆事件，然后填队、清队，最后（`audit=True` 时）用 `status` 数组反推计数做核对。

## 2. 人口生成（`build_students`）

一天的需求量：

```
n = round(cap × visits_per_seat × demand_scale × Gauss(1, daily_demand_jitter))
```

到馆时刻按 14 个小时权重 `HOUR_WEIGHTS`（08:00→22:00，午高峰 0.16、晚高峰 0.10）做离散逆变换，小时内部均匀分布。

每个学生一次性预抽所有随机量，**先于任何机制逻辑**：

| 变量 | 分布 | 含义 |
|---|---|---|
| `present` | LogNormal(ln(mean_present_h×60), σ)，截断 [35, 540] 分钟 | 计划停留时长 |
| `away` | 最多两段午/晚餐（11:20–12:45、17:10–18:40），加泊松个数的短离座（6–18 min）与上课离座（55–115 min） | 离座剧本 |
| `hoard_p` | Beta(μ=2.0, κ=1.4) | 离座时"不释放座位"的概率 |
| `value` | LogNormal(ln 2.6, 0.55) 元/小时 | 学习时间的个人价值 |
| `patience` | Exp(mean 22 min) | 排队耐心 |
| `u_planner` | U(0,1) | 是否提前预约 |

"预抽"不是工程洁癖：它保证同一 `seed` 下换机制时**人口完全相同**，于是机制之间的差异不是抽样噪声（common random numbers）。

多日聚合时，`run()` 先由主种子派生出每一天独立的子种子，再逐日推进：

```python
master = random.Random(p.seed)
day_seeds = [master.getrandbits(64) for _ in range(days)]
```

这样天与天之间互不污染，日序号可并行，政策比较依然逐位对齐（`tests/test_engine.py::test_policies_share_the_same_population`）。

## 3. 一天的事件循环

事件按"分钟 → 事件列表"挂载，同一分钟内按插入顺序处理（`events: dict[int, list[(kind, sid)]]`）。

```
for now in 0..839:
    snapshot(present, held, booked)      # 座位-分钟计数
    for event in events[now]:
        book / expire / arrive / away / return / chk / depart
    fill_queue(now)                      # 有空位就按 FIFO 放人
    expire_patience(now)                 # 超时放弃 → balked
    patrol_sweep(now)                    # 巡场：now % interval == 0 时清超时 HELD
    if audit: 反推计数并核对
```

关键规则：

- **到达**（`arrive`）：若持有 `BOOKED` 座位则直接转为 `PRESENT`；否则从空闲池取座，取不到就进 FIFO 队列，记录 `arrival + patience` 为放弃截止。
- **离座**（`away`）：按 `hoard_p`（信用机制下再乘 `1 − 0.45·min(violations,2)`）决定是否把座位切成 `HELD`。只有切成 `HELD` 且当前占用率 ≥ `trigger` 时，才挂一个 `now + release` 的 `chk` 事件——这就是"智能释放只在拥挤时才计时"的实现。
- **检查**（`chk`）：只有当座位仍是 `HELD` 且已持续 ≥ `release` 分钟才回收，计 `displaced += 1`（被收回的座位次数），信用机制顺带 `violations += 1`。
- **返回**（`return`）：座位还在（`HELD` 且未被收回）就恢复 `PRESENT`；否则 `violations += 1`，按 `u_rejoin < rejoin_prob` 决定重回队列还是离开。
- **排队与等待口径**（1.1.0 修正）：每个学生一个 `wait_accum` 和 `queue_enter`，入队/离队/超时/收尾时结算，**同一学生多次排队累加而不是重复计算整段在馆时间**；`wait` 是"已入座学生的平均累计排队分钟 / served"，`wait_mean_queued` 是"所有排队分钟 / 排队人数（去重）"，`queue_share = 排队人数 / arrivals` 恒 ≤ 1。
- **巡场**（`patrol`）：管理员每 45 分钟扫一遍，`HELD` 超过 10 分钟未归的座位直接收回（`patrol_grace`），计入 `displaced`、`violations` 与 `patrol_events`；不需要学生自己点"暂离"，因此它检验的是"有人管"与"没人管"的差别，而不是计时器参数。
- **预约**（`book` / `expire`）：提前 40 分钟锁定预约区座位，`arrival + checkin` 分钟未到场自动释放（幽灵预约），计入 `ghost_waste`。

`fill_queue` 只从队首放人，保证 FIFO；队首拿不到座就整体等待，不跳过（避免"插队"造成的虚假高服务率）。

## 4. 指标与福利

```
util            = Σ present座位·分钟 / (cap × open_minutes)
peak_util       = 最忙一小时的 util
present_h       = Σ present / 60            # 真正被学习的座位小时
held_h          = Σ held / 60               # 被占却不学的座位小时（外部性）
empty_share     = held_h / (present_h + held_h)
wait            = Σ 已入座者的累计排队分钟 / served   # 按学生，不按排队次数
wait_mean_queued= Σ 所有排队分钟 / 排队人数（去重）
wait_p50/p90    = 排队分钟的 50/90 分位（含 0，反映"多数人没排过队"）
study_p10/p50   = 每个学生自习分钟（含未入座者的 0）的 10/50 分位
queue_share     = 排队人数 / arrivals                # 恒 ≤ 1
served_rate     = served / arrivals         # arrivals 不含 no_go 与幽灵
fail_rate       = 首访未入座人数 / arrivals
gini            = 学生自习时长的基尼系数
```

福利（元/天）：

```
W = Σ_study  (minutes/60 × value)                # 学习收益
  + Σ_no_go  (present/60 × value × alt_value_frac)  # 被价格挤出者的替代环境收益
  - wait_disu × Σ (wait/60 × value)              # 排队时间的机会成本
  - monitor_cost_per_seat_day × cap              # 秩序维护成本
```

**收入是转移支付，不计入福利**（`revenue_day` 单独报告）。这一点在 `tests/test_policies.py::test_welfare_decomposition_adds_up` 里有断言：`welfare = study_value − wait_cost − monitor`。

## 5. 九种机制的形式化

| key | 规则 | 参数 |
|---|---|---|
| `fcfs` | 纯先到先得，只有离馆才释放 | — |
| `t30` / `t15` | 任何离座超过 N 分钟即回收 | `release=30/15`, `trigger=0` |
| `smart` | 仅当占用率 ≥ 90% 时才武装 N 分钟计时器 | `release=30`, `trigger=0.9` |
| `patrol` | 管理员每 45 分钟巡场，离座超 10 分钟即清座（无计时器） | `staff_patrol=True`, `patrol_interval=45`, `patrol_grace=10` |
| `res` | 全部座位可提前 40 分钟预约，到场 15 分钟内有效 | `reserve_share=1.0`, `planner_share=0.85`, `checkin=15`, `no_show=0.18` |
| `mix` | 30% 预约区 + 其余座位智能释放 | `reserve_share=0.30`, `planner_share=0.30`, `release=30`, `trigger=0.9` |
| `credit` | 30 分钟释放 + 违规次数折减占座倾向 | `credit=True`, `credit_effect=0.45` |
| `price` | 计时收费 p 元/小时，价值低于 p 的学生不到馆 | `price=2.0` |

`Policy` 是 frozen dataclass，任意字段可覆盖；`resolve_policy("smart", release=20)` 得到"20 分钟 + 90% 触发"的组合，无需新增机制。

## 6. 外部校验：M/M/c

座位＝服务台，因此可以用教科书模型独立校验。`queueing.erlang_c` 在对数域求值（`c!` 在 c≈171 就溢出 float），并做 log-sum-exp：

```
C(c, a) = [a^c / (c!(1-ρ))] / [Σ_{k<c} a^k/k! + a^c/(c!(1-ρ))],  a = λ/μ, ρ = a/c
Wq      = C(c, a) / (cμ − λ)          # 全部以"分钟"为单位
```

另一条独立路径 `simulate_mmc` 用事件堆 + 空闲服务台栈，完全不共享主引擎代码。5 组对照（含 c=60、ρ=0.9）：

| c | ρ | 平均服务(min) | Erlang-C | 独立仿真 | 绝对误差 |
|---|---|---|---|---|---|
| 1 | 0.50 | 60 | 60.00 | 60.60 | 0.60 |
| 6 | 0.80 | 69 | 29.77 | 29.71 | 0.06 |
| 8 | 0.85 | 66 | 31.49 | 30.39 | 1.11 |
| 20 | 0.75 | 60 | 1.93 | 1.96 | 0.04 |
| 60 | 0.90 | 55 | 2.98 | 2.92 | 0.06 |

复现：`seatlab validate --n 200000`。

单位陷阱记录在案：早期版本在 `mmc_wait` 里多乘了 60（把"每分钟"当成"每小时"），被这组校验抓出（理论值 441 分钟 vs 仿真 2.6 分钟）。这也是保留两套实现的原因。

## 7. 审计模式

`simulate_day(..., audit=True)` 在每分钟做三件事：

1. 用 `status` 数组反推 `present/held/booked`，与增量计数器逐一比对；
2. 检查 `present + held + booked ≤ cap`；
3. 结束时核对"空闲座位数 = 空闲表长度"，并检查空闲表无重复 id。

任何一条不符就写入 `audit_errors`。测试对 9 种机制逐个断言为空，这是"没有算错账"的机器可验证版本。

## 8. 标定与外部效度

**两步标定**。第一步是"默认值"：`visits_per_seat = 2.03`、`mean_present_h = 4.2`（平时）/ `4.9`（期末）、`demand_scale = 1.4`，来自公开的高校图书馆年度进馆人次与在馆时长统计区间，取中位情形。第二步是"换校重标"：`seatlab calibrate` 用馆方已有的三项观测——每天到馆人次、空占占比、人均自习时长——反解 `visits_per_seat`、`mean_present_h`、`hoard_mu`：

```
for axis in (visits_per_seat, mean_present_h, hoard_mu):
    lo, hi = 物理下界, 物理上界
    repeat 7 次:                       # 确定性二分
        mid = (lo + hi) / 2
        m = simulate(Params(axis=mid), fcfs, demand)
        if m[对应观测] < target: lo = mid else: hi = mid
    取中点写回
最后一轮用 arrivals 残差做一次比例修正（|残差| < 1% 时跳过）
```

合成实验（真值 1.6 / 5.2 h / μ=1.6，需求 1.2，seed 777，6 天）恢复出 1.600 / 5.168 h / 1.640（μ），残差 `arrivals 0.00%`、`empty 0.0002`、`study 0.006 h`，共 16 次仿真，约 1 秒。因为不用随机优化器，同样的观测值永远得到同样的参数——这一点有测试守着。
- `hoard_mu/hoard_kappa` 使占座倾向均值 ≈ 0.59（Beta(2.0, 1.4)）。
- 时间价值中位数 2.6 元/小时，可按 `pricing` 端点改写。

**这不是统计推断**：所有参数都是显式输入，`Params` 的每个字段都能替换。仓库的价值在于"给定这套参数，规则之间差多少"是可计算的，而不是"全国一年浪费 15 亿小时"这类数字本身。

## 9. 复现全部实验

```bash
seatlab study --days 40 --sweep-days 16 --out examples/results.json
```

输出（`examples/results.json`）包含：两情景 × 9 机制、占座强度 × 需求热力图（6×5）、容量弹性（7 档 × 4 机制）、座位影子价格、机制折算的等效座位数、40 格优化网格、9 点价格曲线、12 点价格敏感性、全国账本外推、以及全部元数据（seed / 天数 / 版本）。

`seatlab sweep` 是同一内核的网格版：容量 × 需求任意组合（≤80 格）单机制扫描，可直接出 CSV。

同 seed 同参数输出逐字节相同（`tests/test_study.py::test_full_study_is_reproducible`）。
