# 双端部署：控制台 + 学生端

1.2.0 起，SeatLab 不只是一个仿真 API，还是一套可以直接交给图书馆运维的**有状态服务**；1.3.0 又补上了统一认证与 PostgreSQL 多实例。

```
  馆员 / 大屏 ──┐   ┌─────────────────────────────────────┐
                 ├──▶│ /api/v1/admin/*   (/admin 控制台)   │──┐
  学生 / 手机 ──┘   │ /api/v1/app/*     (/app 学生端)     │  │
                    └─────────────────────────────────────┘  ├── OpsService ── Store
                         ▲            ▲                       │      ├─ SQLite（单机，单文件）
                         │            │                       │      └─ PostgreSQL（多实例）
                    ┌────┴─────┐ ┌────┴──────┐                │
                    │ CAS 2.0  │ │ OAuth2/OIDC│  ← 可选，替代 X-Student-ID
                    └──────────┘ └───────────┘                │
```

仿真内核（`/api/v1/simulate` 等）保持无状态；只有双端操作走 Store。二者共用同一个进程，`seatlab serve --db /data/seatlab.db` 一条命令即可；多实例改用 `postgresql://` DSN。认证细节见 [`AUTH.md`](AUTH.md)。

## 1. 座位状态机

```
 free ──reserve──▶ reserved ──checkin(code)──▶ occupied
   ▲                  │                           │
   │                  │ 15 分钟未签到（no_show）    │ away(≤30min)
   │                  ▼                           ▼
   └────release──── released ◀──away 超时──── away
```

| 状态 | 含义 | 谁可以改变 |
|---|---|---|
| `free` | 可预约 | 系统 / 释放 / 回收 |
| `reserved` | 已锁定，等待签到 | 学生签到、超时回收、本人释放、被举报 |
| `occupied` | 在座 | 暂离、释放、闭馆清场 |
| `away` | 暂离计时中 | 回来、超时回收 |

每一次非正常结束都会写一条信用记录：

| 事件 | 分值 | 触发 |
|---|---|---|
| `no_show` | 1 | 预约后 15 分钟（可配置）未签到 |
| `away_overrun` | 2 | 暂离超过上限未回 |
| `reported_hoarding` | 2 | 被他人举报且确实未签到 |

同一学生在 30 天窗口内累计 ≥ 3 分（可配置）即暂停预约 7 天；馆员可在控制台**豁免**任意一条记录（申诉通道）。

## 1.1 预约生命周期

- `reserved` 超过签到宽限（默认 15 分钟）→ `expired`，释放座位并记 no-show；
- `checked_in` 到 `expires_at` → `released`（正常结束，不记违规）；
- `away` 超过暂离上限 → `released` 并记违规；
- 非开馆时段仍存在活跃预约 → 自动清场：`reserved → expired`，`checked_in/away → released`，原因 `venue_closed`；
- 所有业务读取都会先结算到期预约，服务离线跨夜后也会在第一次请求时清理。

## 2. 控制端 `/api/v1/admin/*`

鉴权顺序：`X-Admin-Key`（应急密钥，可留空）→ 馆员 SSO 会话（`role=admin`，需 `X-CSRF-Token`）。`cas/oauth` 模式下未设置 Admin Key 时**不会**放行匿名请求；只有 `header` 开发模式才允许完全开放。

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/dashboard` | 实时入座率、今日预约/签到/no-show、违规、工单、机制预览 |
| GET/PATCH | `/venue` | 场馆名称、容量、开关馆、区域、宽限/暂离/信用参数 |
| GET | `/policies` | 9 种机制目录 + 当前生效机制与覆盖参数 |
| POST | `/policies/activate` | 切换机制（可带 `release/trigger/price/...` 覆盖） |
| POST | `/plan` | 容量 × 需求网格（默认 cap-100/cap/cap+100），直接给排班与扩容建议 |
| GET | `/seats` | 全量座位（可按 zone/status 过滤） |
| GET | `/reservations` | 预约流水；`?active=true` 只返回进行中预约 |
| POST | `/reservations/{id}/release` | 强制释放单条预约，座位立即回到空闲 |
| GET | `/violations` · POST `/violations/{id}/waive` | 信用记录与豁免 |
| GET | `/complaints` · POST `/complaints/{id}/resolve` | 举报工单与处理 |
| GET/POST | `/announcements` | 通知列表 / 发布，学生端立即可见 |
| POST | `/announcements/{id}/deactivate` | 下线通知（学生端立即消失） |
| GET | `/audit` | 审计日志（谁在什么时候改了什么） |
| GET | `/export` · POST `/import` | 整库导出 / 场馆配置导入（迁移、备份、跨校区复制） |
| POST | `/reset-seats?confirm=true` | 重置座位布局（存在进行中预约时拒绝） |
| POST | `/close?confirm=true` | 立即清场：结束全部进行中预约；不修改开闭馆时间 |

`PATCH /venue` 的关键约束：**有进行中预约时不允许改容量或区域布局**（返回 409），改容量会重建座位并按区域比例重新切分（最大余数法，永远加总等于 cap）。`POST /import` 导入新布局同样受此约束，避免预约指向已消失的座位。

`GET /reservations?active=true` 会先结算到期预约，再返回 `seat_label / student_id / state / expires_at`，控制台据此展示“谁在哪、到几点”。`POST /close` 只是立即清场，把 `reserved` 置为 `expired`、`checked_in/away` 置为 `released`，不会把场馆切换成闭馆状态；真正的闭馆仍由 `open_minute/close_minute` 控制。

## 3. 应用端 `/api/v1/app/*`

学生身份用 `X-Student-ID`（或请求体里的 `student_id`）。`SEATLAB_APP_KEY` 非空时，写操作还需要 `X-App-Key`——适合把自助机放在校园网内的场景；公开读接口（座位图、空位、通知）不需要任何密钥。

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/venue` · `/availability` · `/seats` · `/announcements` | 场馆、分区域空位、座位图、通知；`availability` 含 `minutes_to_close` |
| POST | `/reservations` | 预约：`{zone?, seat_id?, minutes?}`；超时或闭馆前不足 30 分钟拒绝，时长自动收缩并返回 `requested_minutes / clamped / close_at` |
| POST | `/reservations/{id}/checkin` | 4 位签到码；错误码 403、超时 409（已被回收） |
| POST | `/reservations/{id}/away` | 暂离 ≤ 上限（默认 30 分钟） |
| POST | `/reservations/{id}/back` | 暂离回来；超时返回 410 |
| DELETE | `/reservations/{id}?student_id=...` | 主动释放 |
| GET | `/me?student_id=...` | 当前预约、信用分、是否封禁、候补详情（区域/位次/加入时间） |
| POST | `/reports` | 举报占座；若对方确实未签到，其预约立即回收并记 2 分 |
| POST/DELETE | `/waitlist` | 加入/退出候补（按区域 FIFO）；加入前校验开闭馆、区域与封禁状态 |

防刷：应用端写操作按「学生 + 动作」做 60 秒滑动窗口限流（`SEATLAB_APP_RATE`，默认 60 次/分钟，举报默认 15 次/分钟），超限返回 429。

## 4. 身份认证与权限

- 默认 `SEATLAB_AUTH_MODE=header`，只信任 `X-Student-ID`，**仅限内网或已验证的反向代理**。
- 公网部署设置 `cas` 或 `oauth`，并配置 `SEATLAB_PUBLIC_URL`、`SEATLAB_SESSION_SECRET`；见 [`AUTH.md`](AUTH.md)。
- 馆员权限来自 `SEATLAB_ADMIN_IDS`（学号/工号）或 IdP 的 `role/groups`；`X-Admin-Key` 作为 IdP 故障时的应急入口。
- 学生会话默认 12 小时，数据库存 token 哈希；`seatlab sessions` / `revoke-sessions` 可审计与强制下线。
- 页面自动处理 CSRF；脚本客户端用 `Authorization: Bearer` 或从 `/auth/me` 取 `csrf` 后带 `X-CSRF-Token`。

## 5. 部署清单

```bash
# 1) 建库（容量、馆名）
seatlab init-db --db /data/seatlab.db --cap 500 --name "XX大学图书馆"

# 2) 启动（SQLite 单机：单 worker；Postgres 可多 worker）
SEATLAB_DB=/data/seatlab.db \
SEATLAB_ADMIN_KEY='换成随机串' \
SEATLAB_APP_KEY='自助机访问码（可空）' \
seatlab serve --host 127.0.0.1 --port 8000 --workers 1

# 2b) 公网 + 校园统一认证（CAS）
SEATLAB_AUTH_MODE=cas SEATLAB_PUBLIC_URL=https://seat.example.edu \
SEATLAB_CAS_BASE=https://cas.example.edu/cas SEATLAB_SESSION_SECRET='...' \
SEATLAB_ADMIN_IDS=20241009 seatlab serve --workers 1

# 2c) 多实例 + PostgreSQL
SEATLAB_DB='postgresql://seatlab:***@db/seatlab' \
SEATLAB_AUTH_MODE=oauth SEATLAB_PUBLIC_URL=https://seat.example.edu \
SEATLAB_SESSION_SECRET='...' seatlab serve --workers 2

# 3) 验收
curl -fsS localhost:8000/healthz
curl -fsS -H "X-Admin-Key: $SEATLAB_ADMIN_KEY" localhost:8000/api/v1/admin/dashboard | head -c 400
curl -fsS localhost:8000/api/v1/app/availability
```

- **卷**：Docker `seatlab-data:/data`；systemd `StateDirectory=seatlab`（`/var/lib/seatlab`）。
- **备份**：`sqlite3 /data/seatlab.db ".backup /backup/seatlab-$(date +%F).db"`，或直接调 `/api/v1/admin/export` 拿 JSON。
- **多副本**：把 `SEATLAB_DB` 指向 PostgreSQL，即可多 worker/多容器；跨实例的座位唯一性与状态迁移由部分唯一索引 + 条件 UPDATE 保证（见 `docs/AUTH.md` §8）。SQLite 仍然是单写者。
- **Postgres 备份**：`pg_dump --no-owner seatlab > /backup/seatlab-$(date +%F).sql`。
- **时区**：服务用本机时区决定「今天」与开闭馆分钟；容器里设置 `TZ=Asia/Shanghai`。

## 6. 演示数据

```bash
seatlab demo --db /tmp/demo.db --students 60
seatlab ops-status --db /tmp/demo.db   # 控制台同款 JSON
seatlab auth-status --db /tmp/demo.db  # 认证模式、存储后端、活跃会话
```

`demo` 会造出 60 个预约（约 9 成签到、3 个暂离、2 个 no-show、2 条举报），用于截图、培训与验收，不会碰仿真接口。

## 7. 与仿真 API 的关系

- 控制台的「机制预览」调用同一个 `run()`，用当前场馆参数模拟**一天**，并同时算 fcfs 基线，因此切换机制后立刻能看到「服务率 / 空占 / 福利」的差异。
- `/admin/plan` 复用 `study.sweep()`，把容量与需求的组合直接给成表格 + 最优格。
- 也就是说：**仿真负责回答「哪种规则更好」，双端负责让规则真正落地并被执行**。
