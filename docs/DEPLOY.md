# 部署

三种方式任选。所有方式都只需要 Python 3.10+；核心仿真零第三方依赖，HTTP 层需要 `fastapi` + `uvicorn`。

---

## 0. 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `SEATLAB_API_KEY` | 空 | 非空时仿真端点强制校验 `X-API-Key` |
| `SEATLAB_CORS_ORIGINS` | `*` | 逗号分隔的来源白名单。生产环境应写具体域名 |
| `SEATLAB_HOST` / `SEATLAB_PORT` | `0.0.0.0` / `8000` | 仅 compose 使用 |
| `SEATLAB_DB` | `:memory:` | SQLite 路径/`sqlite:///...`，或 `postgresql://...` |
| `SEATLAB_ADMIN_KEY` | 空 | 控制台应急密钥；SSO 模式下可留空 |
| `SEATLAB_APP_KEY` | 空 | 自助机共享码；**SSO 浏览器端不要设置** |
| `SEATLAB_AUTH_MODE` | `header` | `header` / `cas` / `oauth` |
| `SEATLAB_PUBLIC_URL` | 空 | 对外基址；决定 SSO 回调与 Cookie Secure |
| `SEATLAB_SESSION_SECRET` | 空 | CAS/OAuth 必填，签名登录 state |
| `SEATLAB_ADMIN_IDS` | 空 | 馆员学号/工号，SSO 授权用 |

完整清单（含 CAS/OAuth 端点与字段映射）见 [AUTH.md](AUTH.md)；`deploy/seatlab.env.example` 是可复制的模板。

`uvicorn` 的 `--workers` 建议设为 CPU 核数；每个 worker 有独立的 LRU 缓存（1024 条），内存占用约 `workers × 50 MB`。

## 1. Docker（推荐）

```bash
docker compose up -d --build
curl -s localhost:8000/healthz
# {"status":"ok","version":"1.4.0","engine":"discrete-event","storage":"sqlite","schema_version":3,"auth_mode":"cas"}
```

镜像基于 `python:3.12-slim`，非 root 用户（uid 10002）运行，内置 `HEALTHCHECK`，不含编译工具链。

单独构建：

```bash
docker build -t seatlab:1.4.0 .
docker run -d --name seatlab -p 8000:8000 \
  -e SEATLAB_API_KEY=change-me \
  -e SEATLAB_CORS_ORIGINS=https://lib.example.edu.cn \
  seatlab:1.4.0
```

日志：`docker logs -f seatlab`。

## 2. 裸机 + systemd

```bash
sudo useradd -r -s /usr/sbin/nologin seatlab
sudo mkdir -p /opt/seatlab && sudo chown seatlab:seatlab /opt/seatlab
sudo -u seatlab python3 -m venv /opt/seatlab/.venv
sudo -u seatlab /opt/seatlab/.venv/bin/pip install "seatlab[api] @ git+https://github.com/n44897162-eng/seatlab"

sudo cp deploy/seatlab.env.example /etc/seatlab.env
sudo editor /etc/seatlab.env   # 填 DB、PUBLIC_URL、SESSION_SECRET、CAS/OAuth
sudo chmod 600 /etc/seatlab.env

sudo cp deploy/systemd/seatlab.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now seatlab
systemctl status seatlab --no-pager
journalctl -u seatlab -f
```

服务只监听 `127.0.0.1:8000`，由 nginx 对外。

## 3. nginx 反向代理

```bash
sudo cp deploy/nginx/seatlab.conf /etc/nginx/sites-available/seatlab
sudo ln -s /etc/nginx/sites-available/seatlab /etc/nginx/sites-enabled/seatlab
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d seatlab.example.edu.cn   # 可选 TLS
```

配置要点（已在 `deploy/nginx/seatlab.conf` 里写好）：

- `proxy_read_timeout 180s`：`/optimize` 与 `/pricing` 是几十次仿真，40 格约 30–60 秒，默认 60 秒会 504。
- `client_max_body_size 64k`：请求体只有几十个字段，不需要更大。
- `/healthz` 不做访问日志，避免探针刷日志。
- 对 `/api/v1/*` 做 `limit_req` 限流（默认 10 r/s、burst 20）。

## 4. 直接前台运行（调试）

```bash
seatlab serve --host 0.0.0.0 --port 8000 --reload
```

`--reload` 仅开发用；生产环境不要开。

## 5. 验证部署

```bash
# 存活
curl -fsS localhost:8000/healthz

# 文档
curl -fsS localhost:8000/openapi.json | python -c "import json,sys;print(json.load(sys.stdin)['info']['title'])"

# 一次真实仿真（带 Key）
curl -fsS localhost:8000/api/v1/simulate \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: change-me' \
  -d '{"params":{"cap":500,"days":5},"policy":{"key":"smart"},"demand_scale":1.4}' \
  | python -c "import json,sys;m=json.load(sys.stdin)['metrics'];print(m['served_rate'],m['empty_share'])"
```

## 5.1 观测与设计器

```bash
curl -fsS localhost:8000/metrics | head -20          # Prometheus 文本
curl -fsS localhost:8000/design -o /dev/null -w '%{http_code}\n'   # 200
```

`/metrics` 无需鉴权，建议在 Prometheus 里加一条：

```yaml
scrape_configs:
  - job_name: seatlab
    scrape_interval: 15s
    static_configs:
      - targets: ["127.0.0.1:8000"]
```

值得告警的两个指标：`seatlab_errors_total` 增速（500/422 未捕获错误）和 `seatlab_engine_seconds_total` 的增长斜率（估算 CPU 饱和：500 座 × 30 天单次约 30–60 ms）。缓存可以从前两个指标算命中率：`hits / (hits + misses)`。

`/design` 是给馆方现场调参的页面：滑杆改参数 → 浏览器直接 `POST /api/v1/simulate` → 与 fcfs 基线并排显示服务率、空占、排队、福利，以及真实请求体 JSON（可复制去 curl）。它不缓存、不改任何状态，压测时可以直接摘掉这个路由。

## 6. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `401` | 设了 `SEATLAB_API_KEY` 但没带 `X-API-Key` | 加请求头，或清空该变量 |
| `422` + `detail` 是英文模型约束 | 参数组合在物理上不可能（如停留 > 开馆时长） | 按提示改 `open_minutes` 或 `mean_present_h` |
| `422` + `detail` 是数组 | 字段名写错（`extra="forbid"`） | 用 `/docs` 里的 schema |
| `/optimize` 504 | nginx 读超时太短 | 调 `proxy_read_timeout`，或在客户端异步轮询 |
| 内存随请求增长 | 每个 worker 缓存 1024 条结果 | 降低 `maxsize`（`api/app.py`）或减少 workers |
| 结果与同事不一致 | `seed` / `days` / `demand_scale` 不同 | 三者都相同才逐位一致；`/api/v1/meta` 会回显默认值 |
| 换学校后结论不符合直觉 | 用的是默认标定参数 | 先 `POST /api/v1/calibrate`（或 `seatlab calibrate`）用本校三项观测值重标 |

## 6.1 双端部署与统一认证（1.4.0+）

```bash
seatlab init-db --db /data/seatlab.db --cap 500 --name "XX大学图书馆"
SEATLAB_DB=/data/seatlab.db SEATLAB_ADMIN_KEY=... seatlab serve --port 8000
```

公网部署必须切到 `cas` 或 `oauth`，并设置 `SEATLAB_PUBLIC_URL` 与 `SEATLAB_SESSION_SECRET`：

```bash
SEATLAB_AUTH_MODE=cas \
SEATLAB_PUBLIC_URL=https://seat.example.edu \
SEATLAB_CAS_BASE=https://cas.example.edu/cas \
SEATLAB_CAS_SERVICE=https://seat.example.edu/auth/callback \
SEATLAB_SESSION_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')" \
SEATLAB_ADMIN_IDS=20241009 \
SEATLAB_ADMIN_KEY=break-glass-only \
seatlab serve --port 8000 --workers 1
```

- 控制台：`https://<域名>/admin`；学生端：`https://<域名>/app`。
- 回调地址必须在 IdP 侧登记，且与 `PUBLIC_URL/auth/callback` 完全一致。
- Cookie 必须是 Secure（HTTPS）；nginx 需要转发 `X-Forwarded-Proto` / `X-Forwarded-Host`。
- 单写者：SQLite 适合单实例，`--workers` 保持 1。
- 备份：`sqlite3 /data/seatlab.db ".backup /backup/seatlab.db"` 或 `GET /api/v1/admin/export`。
- 上线自检：`seatlab auth-status --db /data/seatlab.db`；用 `seatlab sessions` 看活跃会话。
- 完整规则、状态机与接口表见 [OPS.md](OPS.md)，认证与多实例细节见 [AUTH.md](AUTH.md)。

## 6.2 PostgreSQL 多实例

```bash
docker compose -f deploy/docker-compose.postgres.yml up -d --build
# 或裸机：
pip install 'seatlab[api,postgres]'
export SEATLAB_DB='postgresql://seatlab:***@db.example.edu:5432/seatlab'
seatlab serve --host 127.0.0.1 --port 8000 --workers 2
```

- 跨实例的座位唯一性与状态迁移由数据库部分唯一索引 + 条件 UPDATE 保证，可安全跑多 worker/多容器。
- 需要 `psycopg`（`seatlab[postgres]` extra）；镜像已内置。
- 备份：`pg_dump --no-owner seatlab > /backup/seatlab-$(date +%F).sql`。
- 1.2 的 SQLite 库升级到 1.3 会自动加 `sessions` 表与索引；若历史数据已有同座多条活跃预约，先清理再启动。

## 7. 升级与回滚

```bash
docker compose pull && docker compose up -d    # 或
sudo systemctl restart seatlab
```

镜像使用固定 tag（`seatlab:1.4.0`），回滚即切回上一个 tag。SQLite 数据库带 schema 版本：1.3 启动时会自动补 `sessions` 表和索引；版本高于程序会拒绝启动。PostgreSQL 同样自动建表。
