#!/usr/bin/env bash
# End-to-end demo against a running SeatLab instance.
#
#   ./examples/curl.sh [base_url]
#
# Requires curl and python3 (only for pretty-printing JSON).

set -euo pipefail
BASE="${1:-http://localhost:8000}"
KEY="${SEATLAB_API_KEY:-}"

auth=()
if [[ -n "$KEY" ]]; then
  auth=(-H "X-API-Key: $KEY")
fi

say() { printf '\n\033[1;36m== %s\033[0m\n' "$1"; }
post() { curl -fsS "$BASE$1" -H 'Content-Type: application/json' "${auth[@]}" -d "$2"; }

say "health"
curl -fsS "$BASE/healthz" | python3 -m json.tool

say "one mechanism (exam week, smart release)"
post /api/v1/simulate '{
  "params": {"cap": 500, "days": 16, "mean_present_h": 4.9},
  "policy": {"key": "smart"},
  "demand_scale": 1.4
}' | python3 -c "
import json, sys
m = json.load(sys.stdin)['metrics']
print('served %.1f%%   held-empty %.1f%%   wait %.1f min   welfare %.0f CNY/day'
      % (m['served_rate'] * 100, m['empty_share'] * 100, m['wait'], m['welfare_day']))"

say "nine mechanisms on the same students"
post /api/v1/compare '{
  "params": {"cap": 500, "days": 16, "mean_present_h": 4.9},
  "policies": ["fcfs", "t30", "smart", "price"],
  "demand_scale": 1.4
}' | python3 -c "
import json, sys
d = json.load(sys.stdin)
print('%s is the baseline; best welfare: %s' % (d['base_key'], d['best_welfare_key']))
for k, v in d['welfare_gain_pct'].items():
    print('  %-6s %+6.2f%%' % (k, v))"

say "capacity x demand sweep (CSV)"
curl -fsS -X POST "$BASE/api/v1/sweep?format=csv" -H 'Content-Type: application/json' "${auth[@]}" -d '{
  "policy": "t15", "caps": [420, 460, 500], "demands": [1.2, 1.4], "days": 8
}' | head -3

say "calibrate from three observable targets"
post /api/v1/calibrate '{
  "params": {"days": 6, "seed": 777},
  "demand_scale": 1.2,
  "days": 6,
  "targets": {"arrivals_per_day": 985.3, "empty_share": 0.0831, "mean_study_h": 4.495}
}' | python3 -c "
import json, sys
d = json.load(sys.stdin)
p = d['params']
print('visits_per_seat %.3f  mean_present_h %.2f  hoard_mu %.2f  runs %d'
      % (p['visits_per_seat'], p['mean_present_h'], p['hoard_mu'], d['runs']))
print('residuals', d['residuals'])"

say "prometheus metrics"
curl -fsS "$BASE/metrics" | grep -E '^seatlab_(info|engine_seconds_total)' || true

say "national seat-time ledger"
post /api/v1/scale-up '{"students_wan": 4301, "universities": 3119}' \
  | python3 -c "
import json, sys
d = json.load(sys.stdin)
lo, hi = d['wasted_hours_yi']
print('held-but-empty seat time: %.1f-%.1f hundred million hours / year' % (lo, hi))"
