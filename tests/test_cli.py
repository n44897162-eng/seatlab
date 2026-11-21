"""CLI smoke tests: every sub-command must run end to end and emit JSON."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


SRC = ROOT / "src"


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return subprocess.run(
        [sys.executable, "-m", "seatlab.cli", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


SMALL = ("--cap", "120", "--days", "2")


def test_version() -> None:
    out = run_cli("--version")
    assert out.returncode == 0
    assert "seatlab 1.4.0" in out.stdout


def test_simulate_json() -> None:
    out = run_cli("simulate", *SMALL, "--preset", "exam", "--policy", "smart")
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout)
    assert payload["key"] == "smart"
    assert 0 <= payload["served_rate"] <= 1


def test_simulate_table() -> None:
    out = run_cli("simulate", *SMALL, "--policy", "fcfs", "--table")
    assert out.returncode == 0, out.stderr
    assert "welfare" in out.stdout


def test_compare_table_lists_every_mechanism() -> None:
    out = run_cli("compare", *SMALL, "--table")
    assert out.returncode == 0, out.stderr
    rows = [line for line in out.stdout.strip().splitlines() if line.startswith("fcfs")]
    assert rows and "price" in out.stdout


def test_optimize_and_out_file(tmp_path: Path) -> None:
    target = tmp_path / "grid.json"
    out = run_cli("optimize", *SMALL, "--out", str(target))
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout)
    assert len(payload["rows"]) == 40
    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["best_welfare"]["welfare_day"] > 0


def test_pricing() -> None:
    out = run_cli("pricing", *SMALL)
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout)
    assert len(payload["rows"]) == 9


def test_validate() -> None:
    out = run_cli("validate", "--n", "20000")
    assert out.returncode == 0, out.stderr
    rows = json.loads(out.stdout)
    assert len(rows) == 5
    assert all(row["abs_error_min"] <= 3.0 for row in rows)


def test_scale_up() -> None:
    out = run_cli("scale-up")
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout)
    assert payload["wasted_hours_yi"][0] == pytest.approx(14.98, abs=0.05)


def test_study_writes_a_file(tmp_path: Path) -> None:
    target = tmp_path / "results.json"
    out = run_cli("study", "--days", "2", "--sweep-days", "1", "--out", str(target))
    assert out.returncode == 0, out.stderr
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert "scenarios" in payload and "national" in payload


def test_invalid_policy_is_a_usage_error() -> None:
    out = run_cli("simulate", "--policy", "nope")
    assert out.returncode == 2


def test_invalid_params_are_reported() -> None:
    out = run_cli("simulate", "--cap", "5")
    assert out.returncode == 2
    assert "error:" in out.stderr


def test_calibrate_subcommand() -> None:
    result = run_cli(
        "calibrate",
        "--days",
        "4",
        "--seed",
        "5150",
        "--demand",
        "1.2",
        "--arrivals",
        "700",
        "--empty",
        "0.09",
        "--study-hours",
        "4.6",
        "--iterations",
        "5",
    )
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    assert "visits_per_seat" in body["params"]
    assert body["runs"] > 0


def test_sweep_subcommand_csv(tmp_path: Path) -> None:
    out = tmp_path / "grid.csv"
    result = run_cli(
        "sweep", "--caps", "300", "500", "--demands", "1.1", "1.4", "--days", "5", "--csv", str(out)
    )
    assert result.returncode == 0, result.stderr
    lines = out.read_text().splitlines()
    assert lines[0].startswith("cap,demand")
    assert len(lines) == 5
    assert "best" in json.loads(result.stdout)


def test_auth_status_and_session_revoke(tmp_path: Path) -> None:
    db = tmp_path / "ops.db"
    assert run_cli("init-db", "--db", str(db)).returncode == 0
    from seatlab.store import Store

    store = Store(str(db))
    for i in (1, 2):
        store.create_session(
            {
                "token_hash": f"hash-{i}-" + "a" * 60,
                "student_id": "20241001",
                "role": "student",
                "csrf": f"csrf-{i}",
                "created_at": f"2026-11-21T1{i}:00:00",
                "expires_at": "2026-11-22T10:00:00",
            }
        )
    store.close()

    out = run_cli("auth-status", "--db", str(db), "--json")
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout)
    assert payload["auth"]["mode"] == "header"
    assert payload["active_sessions"] == 2

    listing = run_cli("sessions", "--db", str(db))
    assert listing.returncode == 0, listing.stderr
    assert "20241001" in listing.stdout
    assert ("hash-1-" + "a" * 60) not in listing.stdout  # only a 12-char prefix is printed

    revoked = run_cli("revoke-sessions", "--db", str(db), "--student", "20241001")
    assert revoked.returncode == 0, revoked.stderr
    assert json.loads(revoked.stdout)["revoked"] == 2
    listing = run_cli("sessions", "--db", str(db))
    assert "20241001" not in listing.stdout
