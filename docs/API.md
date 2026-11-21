# HTTP API

> 有状态的双端接口（`/api/v1/admin/*`、`/api/v1/app/*`）单独成章，见 [OPS.md](OPS.md)；校园统一认证见 [AUTH.md](AUTH.md)。本文件描述无状态仿真 API，并列出认证/会话端点。

Base URL 默认 `http://localhost:8000`。交互式文档在 `/docs`（Swagger UI），机器可读的 OpenAPI 在 `/openapi.json`。

所有请求体都是 JSON，`extra="forbid"`：写错字段名会直接 422，而不是被静默忽略。所有时长以**分钟**为单位（字段名带 `_h` 的除外），金额为人民币。

## 认证

默认不校验。设置环境变量 `SEATLAB_API_KEY=...` 后，`/api/v1/*` 的每个 POST 都必须带：

```
X-API-Key: <key>
```

缺失或错误返回 `401 {"detail":"invalid or missing X-API-Key"}`。`/healthz`、`/api/v1/meta`、静态页不受影响。

## 错误语义

| 状态码 | 场景 | 响应 |
|---|---|---|
| 422 | 字段越界、类型错误、未知字段、未知机制名 | FastAPI 标准校验结构 |
| 422 | 校验通过但模型不可能（如停留时长超过开馆时长） | `{"detail": "params.mean_present_h must be shorter than the opening window"}` |
| 401 | API Key 缺失/错误 | `{"detail": "invalid or missing X-API-Key"}` |
| 500 | 未预期错误 | uvicorn 默认 |

---

## GET /healthz

```json
{"status": "ok", "version": "1.4.0", "engine": "discrete-event",
 "storage": "sqlite", "schema_version": 3, "auth_mode": "header"}
```

用于容器 `HEALTHCHECK` 与 k8s 探针，不做任何计算。

## GET /api/v1/meta

返回默认参数（`Params` 全字段）、9 种机制的定义与描述、算法常量（840 分钟、14 小时权重、四种座位状态、福利公式）与限额。客户端可以据此渲染参数表单，不必硬编码。

## POST /api/v1/simulate

一个机制、一个情景。

```bash
curl -s localhost:8000/api/v1/simulate -H 'Content-Type: application/json' -d '{
  "params": {"cap": 500, "days": 30, "mean_present_h": 4.9},
  "policy": {"key": "smart"},
  "demand_scale": 1.4
}'
```

`policy` 只覆盖你写的字段，其余继承 `key` 的预设：

```json
{"policy": {"key": "smart", "release": 20, "trigger": 0.85}}
```

响应（节选，`days=30`、`demand_scale=1.4` 的真实输出）：

```json
{
  "metrics": {
    "key": "smart", "name": "智能释放：占用>90% 才计时 30 分钟",
    "cap": 500, "demand_scale": 1.4, "days": 30,
    "arrivals": 1466.6, "served": 1301.1, "served_rate": 0.8872,
    "fail_rate": 0.1128, "balked": 186.2,
    "util": 0.7957, "peak_util": 0.9665,
    "present_h": 5569.9, "held_h": 394.5, "booked_h": 0.0,
    "empty_share": 0.0661, "wait": 3.17,
    "wait_mean_queued": 6.77, "queue_share": 0.4983,
    "wait_p50": 0.0, "wait_p90": 11.0, "study_p10": 0.0, "study_p50": 3.57,
    "displaced": 163.8, "ghost_waste": 0, "patrol_events": 0,
    "avg_study_h": 3.80, "gini": 0.3643,
    "no_go": 0, "total_demand": 1466.6,
    "welfare_day": 16560.01, "study_value_day": 16872.19,
    "wait_cost_day": 137.18, "monitor_day": 175.0, "revenue_day": 0,
    "fail_by_hour": [0.0, 0.151, ...], "arrivals_by_hour": [422.6, 265.0, ...], "util_profile": [...]
  },
  "welfare_breakdown": {
    "study_value_day": 16872.19, "wait_cost_day": 137.18,
    "monitor_day": 175.0, "welfare_day": 16560.01
  }
}
```

字段含义见 [`METHOD.md`](METHOD.md#4-指标与福利)。

## POST /api/v1/compare

同一批学生、同一个随机流，最多 9 种机制。

```bash
curl -s localhost:8000/api/v1/compare -H 'Content-Type: application/json' -d '{
  "params": {"cap": 500, "days": 16, "mean_present_h": 4.9},
  "policies": ["fcfs", "t30", "smart", "price"],
  "demand_scale": 1.4
}'
```

```json
{
  "rows": [ { ...MetricsOut... }, ... ],
  "base_key": "fcfs",
  "best_welfare_key": "price",
  "welfare_gain_pct": {"fcfs": 0.0, "t30": 2.36, "smart": 2.38, "price": 9.25}
}
```

`base_key` 是 `policies[0]`，`welfare_gain_pct` 是相对它的百分比增益。`price` 的 `arrivals` 会低于其它机制——它是唯一会改变"谁到馆"的机制；`total_demand` 仍然相同。

## POST /api/v1/optimize

释放阈值 × 触发水位的网格搜索，最多 120 格（20×10）。

```bash
curl -s localhost:8000/api/v1/optimize -H 'Content-Type: application/json' -d '{
  "params": {"cap": 500},
  "days": 16,
  "demand_scale": 1.4,
  "release_values": [15, 20, 25, 30, 40],
  "trigger_values": [0.85, 0.9, 0.95]
}'
```

```json
{
  "rows": [{"release": 15, "trigger": 0.85, "served_rate": 0.9667, "welfare_day": 15534.6,
            "empty_share": 0.0593, "wait": 6.35, "displaced": 329.0, "avg_study_h": 3.728}, ...],
  "best_welfare": {"release": 20, "trigger": 0.95, "served_rate": 0.9571,
                   "welfare_day": 15636.7, "empty_share": 0.0716, "wait": 6.25,
                   "displaced": 120.0, "avg_study_h": 3.724},
  "best_service": {"release": 15, "trigger": 0.85, "served_rate": 0.9667, ...},
  "demand_scale": 1.4,
  "days": 16
}
```

`best_welfare` 与 `best_service` 通常**不是同一格**：服务率最高的格子往往把排队拉长，福利最高的格子更保守。这个分歧本身是结论的一部分。

## POST /api/v1/pricing

9 个（最多 12 个）价格点的曲线。

```bash
curl -s localhost:8000/api/v1/pricing -H 'Content-Type: application/json' -d '{
  "params": {"cap": 500}, "days": 16, "demand_scale": 1.4
}'
```

```json
{
  "rows": [{"price": 0.0, "welfare_day": 15380.4, "gini": 0.331, "avg_study_h": 3.652,
            "served_rate": 0.9346, "revenue_day": 0.0, "crowd_out": 0.0, "wait": 6.27}, ...],
  "best": {"price": 1.5, "welfare_day": 16209.6, "gini": 0.285, "avg_study_h": 3.905,
           "served_rate": 0.9952, "revenue_day": 7203.5, "crowd_out": 0.154, "wait": 0.83},
  "gain_vs_free_pct": 5.39
}
```

`crowd_out = no_go / total_demand`：被价格直接排除的人口比例。`revenue_day` 是转移支付，不计入福利。

## POST /api/v1/scale-up

把空占座位小时外推到全国账本。所有假设都是显式参数。

```bash
curl -s localhost:8000/api/v1/scale-up -H 'Content-Type: application/json' -d '{
  "students_wan": 4301, "universities": 3119, "seats_per_student": [0.10, 0.14],
  "empty_exam": 0.0883, "empty_calm": 0.0902
}'
```

```json
{
  "students_wan": 4301.0, "universities": 3119,
  "seats_wan": [430.1, 602.1],
  "empty_exam": 0.0883, "empty_calm": 0.0902,
  "wasted_hours_yi": [15.08, 21.11],
  "yuan_yi": [39.2, 54.9],
  "hours_per_student_year": [35.1, 49.1],
  "assumptions": {"seats_per_student": [0.1, 0.14], "peak_days": 110,
                  "calm_days": 170, "open_hours": 14.0, "value_per_hour": 2.6}
}
```

## POST /api/v1/calibrate

把**可观测**的三项输出（每天到馆人次、空占占比、人均自习时长）反解成**难观测**的三项输入（每座每天访问次数、平均在座时长、占座倾向均值）。用确定性二分，不用随机优化器：同样的观测值永远得到同样的参数。

```bash
curl -s localhost:8000/api/v1/calibrate -H 'Content-Type: application/json' -d '{
  "params": {"days": 6, "seed": 777},
  "demand_scale": 1.2,
  "days": 6,
  "targets": {"arrivals_per_day": 985.3, "empty_share": 0.0831, "mean_study_h": 4.495}
}'
```

```json
{
  "params": {"visits_per_seat": 1.600, "mean_present_h": 5.168, "hoard_mu": 1.64, "...": "..."},
  "achieved": {"arrivals": 985.3, "empty_share": 0.0833, "avg_study_h": 4.489, "served_rate": 1.0, "welfare_day": 12992.9},
  "targets": {"arrivals_per_day": 985.3, "empty_share": 0.0831, "mean_study_h": 4.495},
  "residuals": {"arrivals_per_day_pct": 0.0, "empty_share_abs": 0.0002, "mean_study_h_abs": 0.006},
  "saturated": [],
  "trace": [{"step": "visits_per_seat", "value": 1.6}, "..."],
  "runs": 16, "days": 6, "demand_scale": 1.2
}
```

`targets` 三项都可选，传几项就拟合几项（对应输入按顺序联动，未给的项保持原值）。`saturated` 列出"目标超出可行域、拟合只能贴边"的输入名（如 `visits_per_seat` 被 10.0 的上界卡住），此时 `residuals` 会明显不为零——这是数据告诉你"这个观测值在这套容量下不可能"，而不是静默返回一个看似收敛的角点。`hoard_kappa` 是把"占座倾向均值"映射到 Beta 形状的固定参数，默认 1.4，保持一致才能跨校可比。

## POST /api/v1/sweep

容量 × 需求网格。`caps`、`demands` 各 1–10 项，乘积 ≤ 80，`days` ≤ 60。

```bash
curl -s localhost:8000/api/v1/sweep -H 'Content-Type: application/json' -d '{
  "policy": "t15",
  "caps": [420, 460, 500],
  "demands": [1.2, 1.4],
  "days": 12
}'
```

返回 `rows`（每格 `cap / demand / served_rate / empty_share / wait / util / peak_util / welfare_day`）与 `best`（福利最高的一格）。

同样的请求加 `?format=csv` 会得到可直接进 Excel 的 CSV（`Content-Disposition: attachment; filename="seatlab-sweep.csv"`）：

```bash
curl -s -X POST 'localhost:8000/api/v1/sweep?format=csv' -H 'Content-Type: application/json' \
  -d '{"caps":[420,460,500],"demands":[1.2,1.4],"days":12}' -o sweep.csv
```

## GET /metrics

Prometheus 文本格式（`text/plain; version=0.0.4`），无需鉴权，适合被 Prometheus 每 15 秒抓取：

```
seatlab_uptime_seconds 812.4
seatlab_requests_total{method="POST",status="200"} 42
seatlab_errors_total 1
seatlab_cache_hits_total 30
seatlab_cache_misses_total 12
seatlab_engine_seconds_total 6.83
seatlab_requests_by_path{path="/api/v1/simulate"} 25
```

`seatlab_engine_seconds_total` 只累计真正跑仿真的时间（缓存命中不算），可以据此估容量：单次 500 座 × 30 天约 30–60 ms。

## 统一认证端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/auth/config` | 当前模式、是否 SSO、登录地址、是否仍需 Admin Key；不含任何密钥 |
| GET | `/auth/login?next=/app` | 302 到 CAS/OAuth；同时下发签名的 state Cookie |
| GET | `/auth/callback` | CAS `ticket` 或 OAuth `code` + `state`；成功 303 回 `next` 并下发会话 Cookie |
| GET | `/auth/me` | 会话信息 `{authenticated, student_id, role, name, csrf, via}`；头部模式也会回显 `X-Student-ID` |
| POST | `/auth/logout` | 撤销当前会话并清除 Cookie |

`/api/v1/app/config` 额外告诉学生端「是否需要 App Key」，页面据此显示访问码输入框。

用 Cookie 认证的写请求必须带 `X-CSRF-Token`（来自 `/auth/me`）；`Authorization: Bearer <token>` 不受 CSRF 限制。头部开发模式返回 404 于 `/auth/login`。

## 观测头

所有响应都带：

| 头 | 含义 |
|---|---|
| `X-Request-ID` | 请求 id；客户端传入则原样回传，否则服务端生成 |
| `X-SeatLab-Runtime-Ms` | 本请求在服务端耗时（毫秒） |
| `X-SeatLab-Cache` | `hit` / `miss`（仅仿真类端点） |

## GET /design

交互式策略设计器（静态页）：滑杆改容量、需求、释放阈值、触发水位、价格、巡场间隔、天数、停留时长，页面直接 `POST /api/v1/simulate` 拿真实结果，与 fcfs 基线并排显示 KPI、占用曲线和实际请求体 JSON。适合给馆方开会时现场调参。

## 参数速查（ParamsIn）

| 字段 | 默认 | 范围 | 说明 |
|---|---|---|---|
| `cap` | 500 | 10–5000 | 座位数 |
| `visits_per_seat` | 2.03 | 0–10 | 每座每天到达人次 |
| `mean_present_h` | 4.20 | 0.5–13 | 计划停留时长（小时） |
| `sigma_present` | 0.55 | 0–2 | 停留时长对数正态 σ |
| `mean_away_min` | 45 | 0–240 | 午/晚餐离座均值（分钟） |
| `n_away` | 1.5 | 0–4 | 每天离座段数 |
| `n_short` / `n_class` | 1.2 / 0.40 | ≥0 | 短离座 / 上课离座期望次数 |
| `hoard_mu` / `hoard_kappa` | 2.0 / 1.4 | >0 | 占座倾向 Beta 的 α/β |
| `patience_min` | 22 | 1–240 | 排队耐心均值 |
| `rejoin_prob` | 0.90 | 0–1 | 座位被收后重回队列概率 |
| `value_median` / `value_sigma` | 2.60 / 0.55 | >0 | 时间价值（元/小时） |
| `wait_disu` | 0.55 | 0–5 | 排队成本折减 |
| `monitor_cost_per_seat_day` | 0.35 | 0–20 | 每座每天秩序成本 |
| `alt_value_frac` | 0.45 | 0–1 | 被挤出者的替代环境价值 |
| `open_minutes` | 840 | 60–840 | 开馆时长 |
| `days` | 30 | 1–365 | 平均天数 |
| `seed` | 20261121 | 0–2³²−1 | 随机种子 |
| `daily_demand_jitter` | 0.06 | 0–0.5 | 每日需求抖动 |

`PolicyIn` 的 `key` 必须是九种预设之一（`fcfs`/`t30`/`t15`/`smart`/`patrol`/`res`/`mix`/`credit`/`price`），其余字段按需覆盖：

| 字段 | 默认 | 作用 |
|---|---|---|
| `release` / `trigger` | 0 / 0 | 离座多少分钟回收；占用率 ≥ trigger 才武装计时器（0 = 始终计时） |
| `reserve_share` / `planner_share` | 0 / 0 | 预约区比例；提前规划（可预约）的学生比例 |
| `checkin` / `no_show` | 15 / 0.18 | 到馆后多少分钟内必须签到；幽灵预约比例 |
| `price` | 0 | 元/小时，价值低于价格的学生不到馆 |
| `credit` / `credit_effect` | false / 0.45 | 是否按违规次数折减占座倾向；折减强度 |
| `staff_patrol` / `patrol_interval` / `patrol_grace` | false / 45 / 10 | 是否有管理员巡场；巡场间隔；离座多少分钟后可清 |

## 性能

单次 `simulate`（500 座、30 天）实测约 **0.78 秒**（6 天约 0.15 秒；纯标准库单线程）。结果按 `(params, policy, demand, days)` 做进程内 LRU 缓存（maxsize 1024），重复请求即时返回，响应头 `X-SeatLab-Cache: hit` 可以确认。`optimize`/`pricing`/`sweep` 是 N 次 `simulate`：40 格约 30–60 秒，80 格约 1–2 分钟；生产环境请把 nginx 的读超时调到 180 秒以上，或在前端做异步轮询。
