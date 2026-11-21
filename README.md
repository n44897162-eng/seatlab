# 一个座位值多少钱？

**SeatLab** 把高校图书馆的"占座"问题做成了一个可部署的离散事件仿真系统：座位＝固定的服务台，学生＝带停留时长、计划性离座与占座倾向的到达流。它回答三个问题：

1. 你到底被挡在门外多少人次？（服务率、首访失败率、排队分钟——按学生累计，不按排队次数重复计）
2. 被"人不在、座还占着"浪费掉多少座位小时？（`held_h`、`empty_share`）
3. 如果换成另一套规则——离座释放、智能释放、预约、混合、信用分、计时收费——整体更容易了吗？增加或损失多少座位？

> 核心运行时**零第三方依赖**（纯标准库）。装好之后 `seatlab simulate` 就能跑；只有 `seatlab serve` 需要 FastAPI/uvicorn，`seatlab[api]` 一个 extra 解决。
>
> 1.2.0 起多了一套**可直接交给图书馆运行的双端系统**：馆员用 `/admin` 配场馆、切机制、处理举报与信用申诉；学生用 `/app` 预约、签到、暂离、释放、举报。状态落一个 SQLite 文件，`seatlab init-db` + `seatlab serve --db` 两条命令上线。规则与接口见 [`docs/OPS.md`](docs/OPS.md)。
>
> **1.4.0** 把 1.3.0 的机构级底座推到"能每天开着"的状态：闭馆自动清场、已签到到点自动结束、开馆窗口校验与时长自动收缩；控制台新增进行中预约、强制释放、立即清场、区域编辑器与高级场馆参数；学生端改为内联签到码、可选暂离时长、候补卡片、开馆倒计时、跨区域举报，并让轮询不再打断正在输入的操作。安全上修复了控制台存储型 XSS、CAS 缺少 `RelayState` 的登录 CSRF、畸形 state 导致的 500、过期会话仍被计为活跃等问题。

---

## 为什么不是又一个"座位预约系统"

市面上有一万个小程序在做"预约座位"。这个仓库做的是另一件事：**给分配规则建立可计算、可复现、可反证的后果模型**。

- **同一批学生、同一个随机流**，只换规则，因此差异是规则造成的，不是抽样噪声造成的（paired comparison）。
- **每一分钟做一次账面核对**（`audit=True`）：`present / held / booked` 三类计数必须与 500 个座位数组逐一相符，空闲座位表不得重复、不得超容量。178 个测试就是这个契约的执行者。
- **可以从观测数据反推参数**：`seatlab calibrate` 把"每座每天到馆人次、空占占比、人均自习时长"三个可测输出，反解成"每座访问次数、平均在座时长、占座倾向"三个难测输入（确定性二分，16 次仿真收敛）。
- **两套独立实现互相校验**：闭式 Erlang-C 与独立的 M/M/c 事件仿真对照，误差 < 4%（5 组，含 60 个服务台、ρ=0.9）。

## 30 秒看结论

期末周、500 座、每天约 1,450 人次到达（`examples/results.json`，40 天平均）：

| 机制 | 服务率 | 空占时长占比 | 平均排队 | 人均自习 | 福利(元/天) | 相对现状 |
|---|---|---|---|---|---|---|
| 现状：先到先得 | 86.9% | 8.8% | 3.5 min | 3.75 h | 16,123 | — |
| 离座 30 分钟释放 | 89.7% | 5.3% | 2.9 min | 3.83 h | 16,492 | +2.3% |
| 离座 15 分钟释放 | 90.8% | 3.9% | 2.7 min | 3.85 h | 16,585 | **+2.9%** |
| 智能释放（>90% 才计时） | 89.4% | 6.6% | 3.0 min | 3.83 h | 16,508 | +2.4% |
| 管理员 45 分钟巡场 | 89.8% | 5.4% | 2.9 min | 3.82 h | 16,416 | +1.8% |
| 全预约 + 15 分钟到馆 | 86.7% | 8.8% | 3.7 min | 3.74 h | 16,060 | −0.4% |
| 30% 预约区 + 智能释放 | 89.3% | 6.6% | 3.1 min | 3.83 h | 16,475 | +2.2% |
| 离座 30 分钟 + 信用分 | 89.8% | 5.1% | 2.9 min | 3.83 h | 16,506 | +2.4% |
| 计时收费 2 元/小时 | 100.0% | 9.0% | 0.0 min | 4.36 h | 17,603 | **+9.2%** |

同一套实验在**平时**（需求 ×1.0）里所有人的服务率都是 100%，任何规则都只是在增加排队或成本；智能释放在这个情景里一次都不触发（`displaced = 0`），福利与现状**逐位相同**。这就是它比"一刀切 15 分钟清座"更值得部署的理由：**只在需要时出手**。

> 1.1.0 修正了一个会误导结论的口径错误：旧版把"上午来、午饭后座位被清、再排队 10 分钟"的学生记成排队 160 分钟，且同一学生多次排队被重复累加。改成按学生累计后，先到先得的平均排队从 12.3 分钟降到 3.5 分钟，"释放类机制反而更堵"的假象随之消失——它们现在同时提高服务率、降低排队、提高福利。

顺带一个换算：带宽 500 座的图书馆，期末周每增加一个座位的边际福利约 **32.3 元/天（9,043 元/年）**；15 分钟释放相对现状的价值相当于**凭空多出 14.9 个座位**，智能释放相当于 12.0 个。

结论不是"收费最好"，而是**三种增长曲线完全不同**：释放类机制用一点排队换服务率，预约类机制把公平换成确定性，收费类机制用价格清空队列但会把 30% 的低支付意愿学生直接排除。系统把这三条曲线同时算给你看，而不是替你选一个。

| 概览页 | 策略设计器（滑杆直连真实 API） |
|---|---|
| ![landing](docs/assets/landing.jpg) | ![design](docs/assets/design.jpg) |

| 控制台（馆员） | 学生端 |
|---|---|
| ![admin](docs/assets/admin.jpg) | ![app](docs/assets/app.jpg) |

| 统一认证后的学生端（多预约卡） | 统一认证后的控制台 |
|---|---|
| ![sso student](docs/assets/sso-student.jpg) | ![sso admin](docs/assets/sso-admin.jpg) |

> 表中的数字由本仓库的引擎生成，可用 `seatlab study` 一键重算。`/lab` 页面里的交互曲线来自同一模型的浏览器内核，用于教学演示；`/design` 页面把滑杆直接接到真实 API，改参数即出真结果。

---

## 快速开始

```bash
# 直接装本仓库（未上 PyPI）
pip install "git+https://github.com/n44897162-eng/seatlab"
seatlab simulate --preset exam --demand 1.4 --days 40 --policy smart

# 带 HTTP 服务
pip install "seatlab[api] @ git+https://github.com/n44897162-eng/seatlab"
seatlab serve --port 8000
# → http://localhost:8000/docs  交互文档
# → http://localhost:8000/lab     浏览器实验室
# → http://localhost:8000/design  策略设计器（滑杆 → 实时 API）
# → http://localhost:8000/admin  控制台（需 Admin Key）
# → http://localhost:8000/app    学生端
# → http://localhost:8000/metrics Prometheus 指标
# → http://localhost:8000/        概览页

# 从源码开发
git clone https://github.com/n44897162-eng/seatlab && cd seatlab
python -m venv .venv && . .venv/bin/activate
pip install -e ".[api,dev]"
make test
```

第一次调用 API：

```bash
curl -s localhost:8000/api/v1/simulate \
  -H 'Content-Type: application/json' \
  -d '{"params":{"cap":500,"days":30},"policy":{"key":"smart"},"demand_scale":1.4}' \
  | python -m json.tool | head -30
```

比较九种机制（同一批学生）：

```bash
curl -s localhost:8000/api/v1/compare \
  -H 'Content-Type: application/json' \
  -d '{"params":{"cap":500,"days":16},"demand_scale":1.4}' \
  | python -c "import json,sys;d=json.load(sys.stdin);\
[print(f\"{r['key']:<6} {r['served_rate']:.1%} {r['welfare_day']:>8.0f}\") for r in d['rows']]"
```

## 命令行

| 命令 | 作用 |
|---|---|
| `seatlab simulate` | 单机制单情景，返回完整指标 |
| `seatlab compare` | 多机制同人口对比（`--table` 输出表格） |
| `seatlab optimize` | 释放阈值 × 触发水位网格搜索（8×5=40 格） |
| `seatlab pricing` | 9 个价格点的福利/公平/收入曲线 |
| `seatlab scale-up` | 把空占座位小时外推到全国量级 |
| `seatlab validate` | Erlang-C vs 独立 M/M/c 仿真校验 |
| `seatlab study` | 复现整套论文级实验，写 `results.json` |
| `seatlab calibrate` | 用三个可观测输出反解三个难观测输入 |
| `seatlab sweep` | 容量 × 需求网格扫描（可导出 CSV） |
| `seatlab serve` | 启动 HTTP 服务（懒加载 uvicorn；`--db/--admin-key/--app-key`） |
| `seatlab init-db` | 建库并生成座位（`--db/--cap/--name`） |
| `seatlab demo` | 灌入演示预约/举报/通知，用于培训与验收 |
| `seatlab ops-status` | 以 JSON 打印控制台同款看板 |

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 存活探针（容器编排用） |
| GET | `/api/v1/meta` | 默认参数、机制清单、算法常量、限额 |
| POST | `/api/v1/simulate` | 单机制仿真 |
| POST | `/api/v1/compare` | ≤8 机制对比 + 相对基准的福利增益 |
| POST | `/api/v1/optimize` | 网格搜索（≤120 格） |
| POST | `/api/v1/pricing` | 价格曲线（≤12 点） |
| POST | `/api/v1/scale-up` | 全国账本外推 |
| POST | `/api/v1/calibrate` | 观测值 → 参数标定（带残差与轨迹） |
| POST | `/api/v1/sweep` | 容量 × 需求网格（`?format=csv` 导出） |
| GET | `/metrics` | Prometheus 文本指标（请求数、引擎耗时、缓存命中） |
| GET | `/design` | 交互式策略设计器页面 |
| * | `/api/v1/admin/*` | 控制端：场馆、机制、工单、信用、审计、导出（`X-Admin-Key`） |
| * | `/api/v1/app/*` | 应用端：预约、签到、暂离、释放、举报、候补（`X-Student-ID`） |
| GET | `/admin` · `/app` | 馆员控制台与学生端页面 |

所有响应带 `X-Request-ID` 与 `X-SeatLab-Runtime-Ms`；`/api/v1/*` 支持 `X-API-Key`（`hmac.compare_digest` 恒时比较）。

详细请求/响应字段见 [`docs/API.md`](docs/API.md)，算法与建模见 [`docs/METHOD.md`](docs/METHOD.md)。

## 仓库结构

```
seatlab/
├── src/seatlab/
│   ├── constants.py      # 一天 840 分钟、14 小时到馆权重、四种座位状态
│   ├── rng.py            # Knuth 泊松、给定均值的 Beta 采样
│   ├── queueing.py       # Erlang-C（对数域）、M/M/c 闭式解与独立事件仿真
│   ├── model.py          # Params / Policy（frozen，可做缓存键，带校验）
│   ├── engine.py         # 逐分钟离散事件内核 + 审计模式 + 多日聚合
│   ├── study.py          # compare / optimize / pricing / scale-up / calibrate / sweep
│   ├── cli.py            # 16 个子命令
│   ├── store.py          # SQLite / PostgreSQL 双后端、唯一约束、会话表
│   ├── service.py        # 预约状态机、信用分、工单、候补、审计
│   ├── auth.py           # CAS/OAuth、签名 state、会话、CSRF
│   ├── api/              # FastAPI 工厂、双端路由、pydantic schema、SSO 回调
│   └── static/           # 概览页、浏览器实验室、策略设计器、控制台、学生端
├── tests/                # 178 个测试：不变量、配对、机制效应、队列、标定、API、SSO、多实例
├── docs/                 # 方法、API、部署、双端运维、统一认证
├── deploy/               # systemd 单元 + nginx 反代
├── examples/             # curl / Python 客户端 / 基准结果
├── Dockerfile
└── .github/workflows/ci.yml
```

## 上传前改名（1 分钟）

仓库里的 `OWNER` 是占位符，指向你自己的 GitHub：

```bash
grep -rln "github.com/OWNER/seatlab" . | xargs sed -i 's|github.com/OWNER/seatlab|github.com/<你的用户名>/seatlab|g'
```

涉及 `pyproject.toml`、`README.md`、`CITATION.cff`、`docs/DEPLOY.md`、`deploy/`、`src/seatlab/api/app.py`。
包名 `seatlab` 已在 PyPI 之外独立；若要发布到 PyPI，请改成你自己的前缀。

完整的建仓、推送标签、发布 Release、开 GitHub Pages 与上线前检查清单见
[`docs/GITHUB.md`](docs/GITHUB.md)。

## 双端上线（1.4.0）

```bash
pip install "seatlab[api] @ git+https://github.com/n44897162-eng/seatlab"
seatlab init-db --db /data/seatlab.db --cap 500 --name "XX大学图书馆"
SEATLAB_DB=/data/seatlab.db SEATLAB_ADMIN_KEY='随机串' seatlab serve --port 8000
# → /admin 控制台（馆员）   → /app 学生端   → /docs API
```

公网部署接校园统一认证（CAS 示例）：

```bash
SEATLAB_AUTH_MODE=cas \
SEATLAB_PUBLIC_URL=https://seat.example.edu \
SEATLAB_CAS_BASE=https://cas.example.edu/cas \
SEATLAB_SESSION_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')" \
SEATLAB_ADMIN_IDS=20241009 \
seatlab serve --port 8000 --workers 1
```

多实例（PostgreSQL）：

```bash
pip install "seatlab[api,postgres]"
SEATLAB_DB='postgresql://seatlab:secret@db.example.edu/seatlab' \
SEATLAB_AUTH_MODE=oauth SEATLAB_PUBLIC_URL=https://seat.example.edu \
SEATLAB_SESSION_SECRET='...' seatlab serve --workers 2
```

```bash
seatlab demo --db /tmp/demo.db --students 60   # 造一套可演示的预约/举报/通知
seatlab ops-status --db /tmp/demo.db           # 与看板同款 JSON
```

| 角色 | 能做什么 |
|---|---|
| 馆员（`/admin`） | SSO 或应急 Key 登录；改容量/区域/宽限、切换 9 种机制、看实时座位图、管理进行中预约、强制释放、立即清场、处理举报、豁免信用、发布/下线通知、跑扩容网格、导出整库 |
| 学生（`/app`） | SSO 或学号进入；看空位与开馆倒计时、预约（可点座）、多张预约卡、内联 4 位码签到、5–30 分钟暂离/回来、释放、举报占座、候补排队与离开候补 |
| 运维 | `init-db` / `demo` / `ops-status` / `auth-status` / `sessions` / `revoke-sessions`、`/metrics`、审计日志、SQLite 卷或 Postgres 备份 |

规则细节（状态机、信用分值、封禁窗口、限流）见 [`docs/OPS.md`](docs/OPS.md)。

## 部署

三种方式，见 [`docs/DEPLOY.md`](docs/DEPLOY.md)：

```bash
# 1) Docker
docker compose up -d --build

# 2) 裸机 + systemd
sudo cp deploy/systemd/seatlab.service /etc/systemd/system/
sudo systemctl enable --now seatlab

# 3) 多实例（Postgres overlay）
docker compose -f deploy/docker-compose.postgres.yml up -d

# 4) 前面挂 nginx
sudo cp deploy/nginx/seatlab.conf /etc/nginx/sites-available/seatlab
```

环境变量：

| 变量 | 默认 | 作用 |
|---|---|---|
| `SEATLAB_API_KEY` | 空 | 非空时仿真端点强制 `X-API-Key` 校验 |
| `SEATLAB_ADMIN_KEY` | 空 | 控制台 `/api/v1/admin/*` 的 `X-Admin-Key`，生产必设 |
| `SEATLAB_APP_KEY` | 空 | 学生端写操作的 `X-App-Key`（自助机场景） |
| `SEATLAB_DB` | `:memory:` | 数据库 URL：SQLite 路径/`sqlite:///...`，或 `postgresql://...` |
| `SEATLAB_APP_RATE` | `60` | 学生端每学生每动作每分钟上限 |
| `SEATLAB_CORS_ORIGINS` | `*` | 逗号分隔的允许来源 |
| `SEATLAB_AUTH_MODE` | `header` | `header`（仅内网）/ `cas` / `oauth` |
| `SEATLAB_PUBLIC_URL` | 空 | 对外基址，决定 SSO 回调与 Cookie Secure |
| `SEATLAB_SESSION_SECRET` | 空 | CAS/OAuth 必填，用于签名登录 state |
| `SEATLAB_SESSION_TTL_H` | `12` | 学生会话有效期（小时） |
| `SEATLAB_COOKIE_SECURE` | 自动 | 未设置时按 `PUBLIC_URL` 是否 HTTPS 判断 |
| `SEATLAB_TRUST_HEADER` | `1`（header 模式） | 迁移期是否继续信任 `X-Student-ID` |
| `SEATLAB_ADMIN_IDS` | 空 | 逗号分隔的馆员学号/工号（SSO 授权） |
| `SEATLAB_ADMIN_ROLES` | `admin,librarian,staff` | IdP 角色/组名匹配 |
| `SEATLAB_CAS_BASE` | 空 | CAS 根地址，如 `https://cas.example.edu/cas` |
| `SEATLAB_CAS_SERVICE` | 自动 | 登记的 callback，默认 `PUBLIC_URL/auth/callback` |
| `SEATLAB_OAUTH_AUTHORIZE/TOKEN/USERINFO` | 空 | OAuth2/OIDC 三个端点 |
| `SEATLAB_OAUTH_CLIENT_ID/SECRET` | 空 | OAuth 客户端凭据 |
| `SEATLAB_OAUTH_ID_FIELD` | `sub` | userinfo 中学号字段（如 `preferred_username`） |

## 质量与可复现

- `ruff check` + `ruff format --check` 全绿；`pytest` **178 passed**（约 31 秒，包含内嵌 PostgreSQL 集成测试；缺少 `pgserver` 时 PostgreSQL 用例自动 skip）。
- GitHub Actions 在 Python 3.10 / 3.11 / 3.12 上跑同一套检查。
- `seatlab study` 是确定性的：同 seed、同参数、同数字，输出可直接 diff。
- 内核审计模式：`simulate_day(..., audit=True)` 每分钟核对三类计数与座位数组，测试里对 9 种机制逐个断言 `audit_errors == []`。
- 等待时间按学生累计（`wait_accum`），同一学生多次排队不重复计；`queue_share ≤ 1` 有不变量测试守着。

## 已知局限

- 需求曲线（到馆权重、到馆人次）按"每座 2.03 人次/天、期末×1.4"校准；换学校请用 `seatlab calibrate` 用自己的三项观测值重新标定，而不是照搬默认值。
- 时间是分钟离散的，离座释放的计时精度为 1 分钟。
- 福利函数里的时间价值中位数 2.6 元/小时是假设值，`pricing` 端点允许改。
- 预约机制的"幽灵预约"比例是外生参数，不建模信用惩罚的动态演化（`credit` 机制只用了静态折减）。

## 引用

仓库里有 `CITATION.cff`，GitHub 会显示 "Cite this repository"。学术引用请同时链接 `docs/METHOD.md`（模型与指标口径），方法学上的关键假设都在那里。

## License

MIT。引用本仓库的模型或数字时，请链接到 `docs/METHOD.md`。
