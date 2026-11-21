# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.4.0] — 2026-11-21

### Added — 日常运行与现场体验

- **闭馆生命周期**：预约前校验开馆窗口，闭馆前不足 30 分钟拒绝新预约；时长自动收缩到闭馆时间，返回 `requested_minutes / clamped / close_at`。签到、暂离也按闭馆时间收敛。
- **自动清场**：服务跨夜离线后，第一次业务读取会把非开馆时段的活跃预约结算掉——`reserved → expired`，`checked_in/away → released`，原因 `venue_closed`，不记违规。
- **已签到到期**：`checked_in` 的 `expires_at` 到点自动结束并释放座位，不再永久占用；结算时间使用预约截止时刻，离线补算不会把昨天记成今天。
- **控制台预约管理**：`GET /reservations?active=true`、单条强制释放、`POST /close` 立即清场（二次确认），显示座位、学生、状态、截止时间。
- **区域编辑器**：控制台可增删改 zone key/name/share；服务端校验 key 1–8 位、名称 1–40 字、份额 `(0,1]`，有活跃预约时拒绝改布局。
- **高级场馆参数**：最长预约分钟、同时在约上限、签到宽限、暂离上限、信用统计窗口进入表单。
- **学生端**：内联 4 位签到码、5/10/15/20/30 分钟暂离、开馆状态与距闭馆倒计时、候补卡与离开候补、跨区域按座位编号举报、键盘可选座；8 秒轮询保留选中座位与滚动位置，输入时暂停刷新。
- **身份与审计**：控制台显示「姓名 · 馆员」或「应急密钥」并提供退出；候补加入统一走业务层并写审计。

### Changed

- `GET /api/v1/app/venue` 增加 `max_reservation_minutes / max_active_reservations / reservation_grace_min / away_limit_min`；`availability` 增加 `minutes_to_close`；`/me` 的候补变为带区域、位次、加入时间的对象列表。
- 控制端公告列表改为三列卡片式表格，正文改为 textarea，已下线通知不再显示下线按钮。
- `Store.sessions(active_only=True)` 接受 `now_iso`，CLI `auth-status / sessions` 不再把过期会话计为活跃。

### Fixed

- **存储型 XSS**：控制台把举报内容、学生 ID、审计 detail 直接拼入 `innerHTML`，学生可注入脚本读取管理员 localStorage 中的 Admin Key；所有动态值统一转义（含单引号）。
- **登录 CSRF**：CAS 默认严格要求 `RelayState` 与签名 Cookie 中的 state 一致；只有显式 `SEATLAB_CAS_RELAY_STATE=0` 才允许老 CAS 降级。失败回调会清理 state Cookie。
- **畸形 state 500**：非法 base64、非数字 expiry、非法 UTF-8 统一返回 400。
- **容量/区域变更破坏预约**：改 zones 或导入新布局时，只要存在活跃预约就返回 409；原逻辑只检查 cap。
- **容量规划高亮失效**：`r===d.best` 对反序列化对象永远为 false，改为 `cap/demand` 组合键比较。
- **学生端 mine 竞态**：释放/签到/暂离后先刷新 `mine` 再刷新座位图，避免已释放座位仍显示“我的”。

### Tests

- 178 项：新增 8 项服务生命周期（闭馆 clamp/拒绝、已签到到期、区域与导入阻断、截止时间戳、强制释放/清场、候补详情）、3 项控制端 API、1 项会话过期过滤；CAS 缺少 RelayState 的用例改为默认拒绝并验证显式 opt-out。

## [1.3.0] — 2026-11-21

### Added — 机构可直接上线的认证与多实例

- **校园统一认证**：`SEATLAB_AUTH_MODE=header|cas|oauth`。CAS 2.0 service ticket 校验（含 `RelayState`，兼容不回传 RelayState 的老 CAS），OAuth2/OIDC 授权码 + userinfo；`/auth/login`、`/auth/callback`、`/auth/me`、`/auth/logout`。
- **数据库会话**：随机 token 只存 SHA-256 哈希，默认 12 小时，可跨 worker、重启不丢、可集中撤销；`seatlab auth-status / sessions / revoke-sessions`。
- **CSRF 防护**：Cookie 写请求必须带 `X-CSRF-Token`；Bearer 客户端不受影响；登录 state 使用签名 Cookie 双重提交，`next` 只允许站内相对路径。
- **馆员 SSO**：`SEATLAB_ADMIN_IDS` 或 IdP `role/groups` 授予控制台权限；`X-Admin-Key` 保留为 IdP 故障时的应急入口。
- **PostgreSQL 后端**：`SEATLAB_DB=postgresql://...`，与 SQLite 共用同一套 SQL 与业务代码，新增 `seatlab[postgres]` extra。
- **多实例安全**：活跃预约的座位部分唯一索引、违规 `(reservation_id, kind)` 唯一、条件 `UPDATE ... WHERE status=` 状态迁移、只在座位仍指向该预约时释放；两个 Store 同时抢座只有一个人成功。
- **部署件**：`deploy/docker-compose.postgres.yml`、`deploy/seatlab.env.example`、`.env.example`、systemd 环境文件、Compose 全量认证变量透传。
- **学生端**：同时 2 个进行中预约各显示一张卡片（各自签到码、倒计时、操作）；SSO 登录/退出；通知列表与下线按钮；馆员控制台补「已发布通知」管理。

### Changed

- `/healthz` 增加 `storage`、`schema_version`、`auth_mode`；`/api/v1/meta` 增加存储与认证信息；`/auth/config` 与 `/api/v1/app/config` 供页面判断登录方式与是否显示访问码。
- `create_app()` 支持注入 `auth_config` / `auth_client`，测试用 `httpx.MockTransport` 模拟 IdP。
- SSO 模式下匿名学生写操作返回 401，`X-Student-ID` 不再被信任（可用 `SEATLAB_TRUST_HEADER=1` 灰度）。
- 控制台在只有 SSO 时不会先发一个必然 401 的探测请求；未登录不再产生控制台错误。

### Fixed

- CAS 回调缺少 state 时无法登录（无 RelayState 的 CAS 服务器）。
- `Store` 的 `RETURNING id` 在 SQLite 下取到 `sqlite3.Row` 的错误；Postgres 的 `executemany` 走 cursor。
- 学生端只展示第一条进行中预约，第二张预约卡看不到签到码。
- 控制台公告只能发布、看不到已发布列表，也没有下线入口。

### Tests

- 166 项：新增 12 项 SSO/CSRF、7 项多实例（含 3 项真实 PostgreSQL）、1 项会话运维 CLI；`pgserver` 缺省时 PostgreSQL 用例自动跳过。

## [1.2.0] — 2026-11-21

### Added — 可直接部署的双端系统

- **控制端 `/api/v1/admin/*` + `/admin` 页面**：场馆参数（容量/区域/开关馆/宽限/暂离/信用）、9 种机制切换与参数覆盖、实时看板（入座率、今日预约/签到/no-show、违规、工单、机制预览）、容量规划网格、举报工单处理、信用记录豁免、通知发布、审计日志、整库导出/导入、清场。
- **应用端 `/api/v1/app/*` + `/app` 页面**：分区域空位、座位图、预约（可指定座位/区域/时长）、4 位签到码、暂离/回来、释放、举报占座、候补队列、我的预约与信用状态。
- **持久化**：`store.py`（SQLite/WAL，单文件，容器挂卷即可）；`init-db` 建库、`demo` 灌演示数据、`ops-status` 打印看板 JSON。
- **业务规则**：15 分钟签到宽限、30 分钟暂离上限、no-show/超时/被举报分别记 1/2/2 分，30 天窗口累计 3 分封禁 7 天；有进行中预约时禁止改容量；改容量后按区域比例用最大余数法重建座位，总数恒等于 cap。
- **鉴权与防刷**：`X-Admin-Key` 与 `X-App-Key` 分离，`hmac.compare_digest`；学生端写操作滑动窗口限流（默认 60 次/分钟）。
- **部署件**：systemd 加 `StateDirectory=seatlab` 与 `ReadWritePaths`，Compose 加 `seatlab-data` 卷与三个密钥变量，Dockerfile 增加 `/data` 卷。

### Changed

- `seatlab serve` 新增 `--db / --admin-key / --app-key`；`create_app()` 支持 `db_path/now/admin_key/app_key`，测试可注入时钟。
- `/metrics` 不再统计自身请求；`X-SeatLab-Cache` 只在真正命中/未命中仿真缓存时出现。
- 看板「入座率」只计在座+暂离（不含已预约未到），签到率按 `checked_in_at` 统计，并单列 no-show。

### Fixed

- 管理端页面 gate 标签闭合错误、学生端缺少 `X-App-Key` 时写操作 401 无提示、预约卡片在 reserved 状态下误显示「本次结束」。
- 签到码允许前导零（`0738` 是合法 4 位码），测试不再用整数区间断言。

## [1.1.0] — 2026-11-21

### Fixed — 一个会改变结论的口径错误

- **等待时间按学生累计，而不是按排队片段重复累加**。旧实现用 `now - arrival` 计算排队分钟，导致"上午到馆、午饭后座位被清、再排队 10 分钟"的学生被记成 160 分钟；同一学生多次排队还会被重复累加。现在每个学生维护 `wait_accum + queue_enter`，在入队、离队、超时、收尾四处结算。
  - 先到先得（期末 40 天）平均排队 **12.32 → 3.46 分钟**，福利 **15,498.85 → 16,123.07 元/天**。
  - `n_queued` 改为去重学生数，`queue_share` 从可能 >1 变为恒 ≤ 1，并加了不变量测试。
  - 修正后"释放类机制反而更堵"的假象消失：`t15` 同时把服务率从 86.9% 提到 90.8%、排队从 3.5 降到 2.7 分钟。

### Added

- **`patrol` 机制（此前只有占位字段）**：管理员每 45 分钟巡场，`HELD` 超过 10 分钟即回收；新增 `Policy.staff_patrol / patrol_interval / patrol_grace`、`patrol_events` 计数器、preset 与描述。机制数 8 → 9。
- **分位数指标**：`wait_p50 / wait_p90 / study_p10 / study_p50`，`engine.percentile()`，`simulate_day` 返回 `waits` 供多日池化。
- **`study.calibrate()` + `POST /api/v1/calibrate` + `seatlab calibrate`**：用三项可观测输出（到馆人次、空占占比、人均自习时长）确定性反解三项难观测输入，输出残差与拟合轨迹；16 次仿真收敛，合成实验残差 < 0.1%。
- **`study.sweep()` + `POST /api/v1/sweep` + `seatlab sweep`**：容量 × 需求网格（≤80 格），支持 `?format=csv` 导出。
- **`GET /metrics`**：Prometheus 文本（请求数、错误数、缓存命中、引擎耗时、uptime、按路径计数），无需鉴权。
- **观测头**：`X-Request-ID`、`X-SeatLab-Runtime-Ms`、`X-SeatLab-Cache`；`/api/v1/*` 的 Key 校验改用 `hmac.compare_digest`。
- **`/design` 交互页**：滑杆直接接真实 `/api/v1/simulate`，与 fcfs 基线并排对比，展示 KPI、占用曲线与实际请求体。
- **短开馆日修正**：`_arrival_times` 按 `open_minutes` 截断小时权重，避免 4 小时开馆日把到达全堆到闭馆前。

### Changed

- 基准数据 `examples/results.json` 全量重跑（9 机制、40 天、seed 20261121）：期末影子价格 32.3 元/座/天（9,043 元/年），`t15` 等效于多出 14.9 个座位。
- 测试 105 → 125（124 passed + 1 skipped），覆盖等待口径、巡场、分位数、标定、网格、/metrics、/design、Key 校验。

## [1.0.0] — 2026-11-21

First public release.

### Added

- **Engine** (`seatlab.engine`): minute-by-minute discrete-event simulation of an
  academic library with four seat states (`free / present / held / booked`),
  hoarding-propensity heterogeneity, planned and unplanned absences, FIFO queueing
  with finite patience, reservations with no-show ghosts, and welfare accounting.
- **Pairing guarantee**: each day gets an independent child seed drawn up front, so
  different mechanisms run on bit-identical demand realisations and days are
  order-independent (`common random numbers`).
- **Audit mode**: `simulate_day(..., audit=True)` re-derives counters from the seat
  array every minute and verifies free-list bookkeeping; the test-suite asserts a
  clean audit for all eight mechanisms.
- **Queueing checks** (`seatlab.queueing`): log-domain Erlang-C that survives
  hundreds of servers, closed-form M/M/c wait in minutes, and an independent
  event-simulation implementation used as a second opinion.
- **Mechanisms**: `fcfs`, `t30`, `t15`, `smart` (release timer armed only above a
  occupancy trigger), `res` (full reservation), `mix` (reservation + smart release),
  `credit` (violation score), `price` (hourly fee with demand crowd-out).
- **Experiments** (`seatlab.study`): paired comparison, release × trigger grid
  search, price curve, national seat-time ledger, equivalent-seat conversion, and
  a single `run_study()` that regenerates the whole published ledger.
- **HTTP API** (`seatlab.api`): FastAPI factory with `/healthz`, `/api/v1/meta`,
  `/simulate`, `/compare`, `/optimize`, `/pricing`, `/scale-up`, optional
  `X-API-Key` authentication, CORS, GZip, in-process LRU result cache, and two
  static pages (`/` overview, `/lab` interactive lab).
- **CLI** (`seatlab`): `simulate`, `compare`, `optimize`, `pricing`, `scale-up`,
  `validate`, `study`, `serve`; JSON by default, `--table` for a human view.
- **Packaging**: zero runtime dependencies for the core, `seatlab[api]` and
  `seatlab[dev]` extras, py.typed marker.
- **Ops**: Dockerfile (non-root, healthcheck), docker-compose, systemd unit with
  hardening, nginx reverse proxy tuned for long-running grid searches.
- **Quality**: 105 tests covering invariants, determinism, pairing, mechanism
  effects, queueing validation, API contract, CLI end-to-end, and study shape;
  ruff lint + format gates; GitHub Actions matrix on Python 3.10–3.12 and a
  clean-venv wheel smoke test.
- **Docs**: `docs/METHOD.md` (full model spec and validation), `docs/API.md`
  (endpoint reference with real responses), `docs/DEPLOY.md`, plus a committed
  baseline ledger in `examples/results.json`.

### Notes

- Results were regenerated after the switch to per-day random streams; numbers
  differ from the earlier single-stream prototype at the third significant digit
  (e.g. exam-week `fcfs` welfare 15,430 → 15,499) while the qualitative ordering
  of mechanisms is unchanged.
- `mmc_wait` previously multiplied by 60 (per-hour rates mistaken for per-minute).
  The unit bug was caught by the independent simulation; all rates are now
  explicitly per minute and the function returns minutes.
