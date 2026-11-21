# Examples

| 文件 | 说明 | 重新生成 |
|---|---|---|
| `curl.sh` | 对运行中的实例跑一遍端到端：healthz → simulate → compare → sweep CSV → calibrate → /metrics → scale-up | `bash examples/curl.sh http://localhost:8000` |
| `python_client.py` | 纯标准库客户端，九机制表格 + 网格扫描 + 标定 | `python examples/python_client.py` |
| `results.json` | 论文级基准：两情景 × 9 机制、热力图、容量弹性、影子价格、优化网格、价格曲线、全国外推 | `seatlab study --days 40 --sweep-days 16 --out examples/results.json` |
| `sweep-t15.csv` | 6 容量 × 4 需求、8 天、`t15` 机制的网格（24 格，可直接进 Excel） | 见下 |
| `calibration.json` | 用三项观测把合成真值（1.5 / 5.0 h / μ=1.5）反解回来的完整报告（含 `saturated` 与 `trace`） | 见下 |

```bash
# 网格 CSV
seatlab sweep --policy t15 --caps 420 460 500 540 580 620 \
  --demands 1.0 1.2 1.4 1.5 --days 8 --csv examples/sweep-t15.csv

# 标定报告（先用观测值构造目标，或直接拿馆方统计）
seatlab calibrate --days 6 --seed 777 --demand 1.2 \
  --arrivals 985 --empty 0.083 --study-hours 4.495 > examples/calibration.json
```

所有输出都是确定性的：同 seed、同参数逐字节相同，可以直接 `git diff` 看模型改动带来的差异。
