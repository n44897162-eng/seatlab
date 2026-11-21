# 校园统一认证（CAS / OAuth2）与多实例部署

1.3.0 起，SeatLab 的学生端和控制台都可以接学校现有的统一身份认证。默认的 `X-Student-ID` 头模式只用于内网和开发；对公网部署必须切换到 `cas` 或 `oauth`。

## 1. 三种模式

| `SEATLAB_AUTH_MODE` | 身份来源 | 适用场景 | 安全性 |
|---|---|---|---|
| `header`（默认） | `X-Student-ID` 请求头 | 本机演示、内网、已有反向代理完成认证 | **仅限可信网络**，公网等于没有认证 |
| `cas` | Apereo CAS 2.0/3.0 service ticket | 国内高校最常见的统一认证 | 推荐 |
| `oauth` | OAuth2 授权码 / OIDC userinfo | Keycloak、Authing、企业微信网关、自建 IdP | 推荐 |

`SEATLAB_TRUST_HEADER=1` 可以在迁移期让 CAS/OAuth 部署继续接受 `X-Student-ID`，用于灰度；上线完成后应设为 `0`。默认在 SSO 模式下为 `0`。

## 2. 最小配置

```bash
export SEATLAB_AUTH_MODE=cas
export SEATLAB_PUBLIC_URL=https://seat.example.edu          # 必须与浏览器地址一致
export SEATLAB_CAS_BASE=https://cas.example.edu/cas
export SEATLAB_CAS_SERVICE=https://seat.example.edu/auth/callback
export SEATLAB_SESSION_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')"
export SEATLAB_ADMIN_IDS=20241009,20241010                     # 馆员学号/工号
export SEATLAB_COOKIE_SECURE=1
seatlab serve --port 8000
```

`SEATLAB_CAS_BASE` 用于拼 `/cas/login`；`SEATLAB_CAS_VALIDATE` 默认 `BASE + /serviceValidate`，若学校把校验地址单独发布（例如 `https://cas.example.edu/cas/p3/serviceValidate`）则显式设置。`SEATLAB_CAS_SERVICE` 必须与 CAS 服务端登记的回调完全一致，否则会返回 `INVALID_SERVICE`。

OAuth（以 Keycloak 为例）：

```bash
export SEATLAB_AUTH_MODE=oauth
export SEATLAB_PUBLIC_URL=https://seat.example.edu
export SEATLAB_OAUTH_AUTHORIZE=https://sso.example.edu/realms/campus/protocol/openid-connect/auth
export SEATLAB_OAUTH_TOKEN=https://sso.example.edu/realms/campus/protocol/openid-connect/token
export SEATLAB_OAUTH_USERINFO=https://sso.example.edu/realms/campus/protocol/openid-connect/userinfo
export SEATLAB_OAUTH_CLIENT_ID=seatlab
export SEATLAB_OAUTH_CLIENT_SECRET=...
export SEATLAB_OAUTH_SCOPE="openid profile email"
export SEATLAB_OAUTH_ID_FIELD=preferred_username   # 默认 sub；学校常把学号放在这个字段
export SEATLAB_SESSION_SECRET=...
```

回调地址统一为 `${SEATLAB_PUBLIC_URL}/auth/callback`，需要在 IdP 侧登记。

## 3. 身份字段与馆员权限

| 变量 | 默认 | 说明 |
|---|---|---|
| `SEATLAB_CAS_ID_ATTR` | `user` | CAS `authenticationSuccess` 下的学号字段；也可指向 `attributes` 中的属性名 |
| `SEATLAB_CAS_NAME_ATTR` | `displayName` | 姓名属性 |
| `SEATLAB_CAS_EMAIL_ATTR` | `mail` | 邮箱属性 |
| `SEATLAB_CAS_RELAY_STATE` | `1` | CAS 是否回传 `RelayState`；默认强制校验，老 CAS 才显式设为 `0` |
| `SEATLAB_OAUTH_ID_FIELD` | `sub` | userinfo 中的学号字段 |
| `SEATLAB_OAUTH_NAME_FIELD` | `name` | 姓名 |
| `SEATLAB_OAUTH_EMAIL_FIELD` | `email` | 邮箱 |
| `SEATLAB_OAUTH_ROLE_FIELD` | `role` | 角色字段，支持字符串或数组 |
| `SEATLAB_OAUTH_GROUPS_FIELD` | `groups` | 组字段，支持字符串或数组 |
| `SEATLAB_ADMIN_IDS` | 空 | **按学号/工号**直接授予馆员权限 |
| `SEATLAB_ADMIN_ROLES` | `admin,librarian,staff` | 角色名匹配；组路径 `campus/librarian` 会取最后一段 |
| `SEATLAB_ADMIN_SSO` | `1` | 是否允许 SSO 馆员会话进入 `/api/v1/admin/*` 与 `/admin` |

`X-Admin-Key` 仍然有效，作为 **break-glass** 应急入口：IdP 故障时馆员仍能进控制台。两者可以同时启用，也可以只启用 SSO（不设置 `SEATLAB_ADMIN_KEY`）。

> 注意：CAS 的 `role` 属性如果由用户自行填写就不可信。优先使用 `SEATLAB_ADMIN_IDS` 或 IdP 管理的 `memberOf/groups`。

## 4. 会话、Cookie 与 CSRF

- 登录成功后，服务端生成 32 字节随机 token，**只把 SHA-256 哈希写库**；浏览器拿到的是 HttpOnly Cookie。
- 会话默认 12 小时（`SEATLAB_SESSION_TTL_H`），数据库可查、可撤销、跨进程有效。重启服务不会把学生踢下线。
- Cookie 默认 `SameSite=Lax`、`Path=/`；`SEATLAB_COOKIE_SECURE` 未设置时按 `SEATLAB_PUBLIC_URL` 是否 HTTPS 自动判断。**公网必须为 Secure**。
- 用 Cookie 认证的写请求必须带 `X-CSRF-Token`，值来自 `GET /auth/me` 的 `csrf` 字段。页面已自动处理；脚本客户端建议改用 `Authorization: Bearer <token>`（Bearer 不受 CSRF 影响）。
- 登录 `state` 使用双重提交：签名 Cookie 保存随机 state、登录后跳转路径和 10 分钟过期时间；IdP 回传的 state 必须与 Cookie 一致。CAS 默认把 state 放进 `RelayState` 并要求原样回传，缺少 `RelayState` 直接拒绝，避免攻击者借受害者待登录状态完成登录 CSRF。只有明确不回传 `RelayState` 的老 CAS 才设置 `SEATLAB_CAS_RELAY_STATE=0`，此时会降级为仅签名 Cookie 校验。
- 学生写操作仍按 `学生:动作` 做 60 次/分钟滑动窗口限流（`SEATLAB_APP_RATE`）。

## 5. 页面行为

`/app` 启动时读取 `/auth/config` 与 `/auth/me`：

- `mode=cas/oauth` → 隐藏学号输入，显示「校园账号登录」；已登录则直接进入，顶部显示姓名。
- 会话过期或 401 → 自动回到登录闸门，不会白屏。
- 右上角「退出」调用 `POST /auth/logout`，服务端撤销会话并清 Cookie。
- 同一学生最多 2 个进行中预约，页面按预约逐张卡片展示，各自有签到码、倒计时和操作按钮。

`/admin` 同理：优先走馆员 SSO，`X-Admin-Key` 作为后备；没有馆员角色时明确提示联系管理员。

## 6. 运维命令

```bash
seatlab auth-status --db postgresql://seatlab:***@db/seatlab
seatlab sessions --db /data/seatlab.db            # 只显示 token 哈希前缀
seatlab revoke-sessions --db /data/seatlab.db --student 20241001
seatlab revoke-sessions --db /data/seatlab.db --all --yes
```

`auth-status` 输出当前模式、回调 URL、存储后端、schema 版本与活跃会话数，不打印任何密钥。

## 7. 反向代理与 HTTPS

SSO 回调必须从公网可达，并且 **`SEATLAB_PUBLIC_URL` 与用户实际访问的 scheme/host 完全一致**。Nginx 示例：

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

若无法设置 `SEATLAB_PUBLIC_URL`，服务会从 `X-Forwarded-Proto/Host` 推导，但 CAS 的 `service` 参数对一致性要求很严，生产环境建议显式配置。

## 8. 多实例与 PostgreSQL

SQLite 只允许一个写者，适合单机；多实例（滚动发布、双活、负载均衡）请使用 PostgreSQL：

```bash
export SEATLAB_DB='postgresql://seatlab:secret@db.example.edu:5432/seatlab'
export SEATLAB_AUTH_MODE=oauth
pip install 'seatlab[api,postgres]'
seatlab serve --workers 2
```

跨实例不变量由数据库保证，而不是进程锁：

- `reservations(seat_id) WHERE status IN ('reserved','checked_in','away')` 上的**部分唯一索引**：两个实例不可能同时占住同一座位；冲突返回 409。
- `violations(reservation_id, kind)` 上的**部分唯一索引**：重复的过期扫描不会重复扣分。
- 状态迁移用 `UPDATE ... WHERE status = <旧状态>`，只有第一个实例成功；释放座位时还要求 `seats.reservation_id` 仍指向该预约，避免把刚被别人预约的座位误清空。

因此可以安全地跑 `--workers N` 或多个容器；建议仍然只让一个实例执行“闭馆清场”类批处理（目前没有内置定时清场，清场是馆员手动操作）。

从 1.2 升级：`schema_version` 会自动从 2 升到 3（新增 `sessions` 表与两个部分唯一索引）。**如果旧库里已经存在同一座位的多条活跃预约，建索引会失败**——先人工清理重复预约再启动，或新建库后导出/导入场馆配置。

## 9. 测试覆盖

`tests/test_auth.py` 用 `httpx.MockTransport` 模拟 CAS 与 OAuth IdP，覆盖：

- CAS 登录 → 回调 → `/auth/me` → 预约 → 签到全链路；
- 错误 ticket、伪造 state、过期 state、缺少 `RelayState` 的回调（默认拒绝，显式关闭才放行）；
- SSO 模式下 `X-Student-ID` 被拒绝；
- Cookie 写操作缺 CSRF 返回 403，Bearer 跳过 CSRF；
- OAuth 馆员角色进入控制台、普通学生被拒；
- 退出后会话立即失效；
- 配置接口不泄露 secret。

`tests/test_multi_instance.py` 用两个独立 `Store` 模拟两个 worker，并在安装了 `pgserver` 时对真实 PostgreSQL 跑同一套契约（部分唯一索引、条件迁移、幂等扣分、共享会话）。
