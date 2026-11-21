# 上传到 GitHub

这个目录已经是一个干净的仓库：`main` 上有 `v1.2.0`、`v1.3.0` 与 `v1.4.0` 三个标签，没有数据库、
构建产物或本机路径。下面的步骤把占位符换成你的账号，然后推送。

## 1. 先把 `OWNER` 换成你的 GitHub 用户名

```bash
cd seatlab-system
grep -rl 'OWNER' --exclude-dir=.git .        # 先看会改哪些文件
grep -rl 'OWNER' --exclude-dir=.git . | xargs sed -i 's/OWNER/你的用户名/g'
```

涉及 `pyproject.toml`、`README.md`、`CITATION.cff`、`docs/DEPLOY.md`、`docs/AUTH.md`、
`deploy/`、`src/seatlab/api/app.py`。改完后跑一遍测试与打包，确认没有把 URL 改坏。

## 2. 建仓库并推送

在 GitHub 上新建空仓库（不要勾 README / .gitignore / license），然后：

```bash
git remote add origin git@github.com:你的用户名/seatlab.git
git push -u origin main
git push origin v1.2.0 v1.3.0 v1.4.0
```

## 3. 发布 Release

```bash
gh release create v1.4.0 dist/seatlab-1.4.0-py3-none-any.whl \
  dist/seatlab-1.4.0.tar.gz --generate-notes --title "SeatLab 1.4.0"
```

`--generate-notes` 会从提交与标签生成说明；`CHANGELOG.md` 里有更完整的中文条目。

## 4. 可选：把静态演示放到 GitHub Pages

仓库根目录的 `index.html` 是自包含的演示（纯前端，无后端）。在
**Settings → Pages → Build and deployment** 选择 `Deploy from a branch`，
分支 `main`、目录 `/ (root)`，几分钟后 `https://你的用户名.github.io/seatlab/`
即可打开。它不连接任何服务器，数据只存在浏览器内存里。

## 5. 上线前检查

- [ ] `SEATLAB_SESSION_SECRET` 换成长随机串（CAS/OAuth 模式下不设会直接启动失败）
- [ ] `SEATLAB_ADMIN_KEY` 换成随机串，或只依赖 SSO 角色并留空
- [ ] `SEATLAB_PUBLIC_URL` 写成真实 HTTPS 地址；`SEATLAB_COOKIE_SECURE` 保持自动
- [ ] CAS/OAuth 的回调地址已登记到学校 IdP（`/auth/callback`）
- [ ] `SEATLAB_TRUST_HEADER=0` 且前面没有会伪造 `X-Student-ID` 的反向代理
- [ ] 数据库已备份，`seatlab auth-status` 显示 schema v3
- [ ] 用 `seatlab demo` 的数据在预发环境跑一遍签到、暂离、举报、公告下线
