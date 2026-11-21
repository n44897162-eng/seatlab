# Contributing

短、直接、可验证。这个仓库只有一条硬规则：**任何改动都必须让 `make test` 依然全绿**。

## 开发环境

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[api,dev]"
pre-commit install          # 可选，装 git hook
make lint && make test
make study                  # 重新生成 examples/results.json
```

`ruff` 的行宽是 100，规则集 `E,F,I,UP,B,SIM`；`E501` 交给 formatter。

## 改动前先想清楚的三件事

1. **是否改变了随机流？** `engine.build_students` 里每个学生的随机量都必须在使用前一次性抽完。若在事件循环中途新增 `rng` 调用，机制之间的配对性会被破坏，`test_policies_share_the_same_population` 会失败。加随机量时请加在 `build_students` 里，或者用 `episode[3]` 这类预抽值。
2. **是否改变了计数口径？** 任何新的座位状态都必须同时进入 `audit` 的每分钟核对，否则 `audit_errors` 会静默漏检。
3. **是否改变了默认参数？** `Params` 的默认值对应 `docs/METHOD.md` 的标定表；改默认值必须同步改文档、`examples/results.json` 与 README 的结论表。

## 测试分层

| 文件 | 覆盖 |
|---|---|
| `tests/test_model.py` | 参数校验、策略覆盖、预设不可变 |
| `tests/test_engine.py` | 审计、守恒、确定性、配对、gini、基准快照 |
| `tests/test_policies.py` | 每种机制的定性效应、福利分解 |
| `tests/test_queueing.py` | Erlang-C 性质、单位、独立仿真误差 |
| `tests/test_study.py` | 网格完整性、价格曲线、外推、全量实验结构 |
| `tests/test_api.py` | 端点契约、422/401 语义、静态页 |
| `tests/test_cli.py` | 8 个子命令端到端 |

新增机制时，请同时补：`PRESET_POLICIES` 条目、`POLICY_DESCRIPTIONS` 文案、
`test_policies.py` 中的一条定性断言、以及 `docs/METHOD.md` 的机制表。

## 提交信息

```
<scope>: <what changed>

Why: <one sentence on the mechanism, not the diff>
```

scope 用 `engine` / `api` / `cli` / `study` / `docs` / `deps`。

## 发布

```bash
make clean && make build
# 检查 dist/ 中的 sdist 与 wheel 内容
git tag -a v1.0.0 -m "seatlab 1.0.0"
```

标签前请确认 CI 的 `package` job 通过（它会在干净 venv 里安装 wheel 并跑一次 `simulate`）。

## 不希望收到的 PR

- 引入 numpy/pandas 作为运行时依赖（核心路径要保证零依赖、可在任何机器上复算）。
- 把"福利"改成"收入 + 福利"（收入是转移支付，重复计算会夸大收费机制）。
- 用 `hash()` 或时间戳做种子（不可复现）。
- 在 `/optimize` 里做并发线程池而不设上限（CPU 打满时会把 `simulate` 拖慢）。
