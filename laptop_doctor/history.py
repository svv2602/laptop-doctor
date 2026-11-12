"""History for charts: own samples (memory/swap) + journal-derived series (temperature, hard resets)."""
from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from .util import run, thermal_zones

STATE_DIR = Path.home() / ".local" / "state" / "laptop-doctor"
HISTORY = STATE_DIR / "history.jsonl"


def record(snap) -> None:
    """Добавить замер (вызывается после каждой проверки, раз в несколько минут)."""
    zones = thermal_zones()
    row = {
        "ts": int(time.time()),
        "mem_avail_pct": round(snap.available_pct, 1),
        "swap_used_gb": round(snap.swap_used / 1024 ** 3, 2),
        "psi60": snap.psi_avg60,
        "cpu_temp": zones.get("x86_pkg_temp") or zones.get("TCPU"),
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a") as f:
        f.write(json.dumps(row) + "\n")


def prune(days: int) -> None:
    if not HISTORY.exists():
        return
    cutoff = time.time() - days * 86400
    rows = [ln for ln in HISTORY.read_text().splitlines() if ln.strip() and json.loads(ln).get("ts", 0) >= cutoff]
    HISTORY.write_text("".join(r + "\n" for r in rows))


def load(since_ts: float) -> list[dict]:
    if not HISTORY.exists():
        return []
    res = []
    for ln in HISTORY.read_text().splitlines():
        try:
            row = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if row.get("ts", 0) >= since_ts:
            res.append(row)
    return res


def series(rows: list[dict], key: str) -> list[tuple[float, float]]:
    return [(r["ts"], r[key]) for r in rows if r.get(key) is not None]


def temps_from_journal(since_ts: float, bucket_s: int) -> list[tuple[float, float]]:
    """Температура CPU из журнала hw-watch: p95 по корзинам bucket_s секунд."""
    since = datetime.fromtimestamp(since_ts).strftime("%Y-%m-%d %H:%M:%S")
    out = run(["journalctl", "-u", "hw-watch", "--since", since, "-o", "short-unix", "--no-pager"], timeout=60) or ""
    buckets: dict[int, list[int]] = defaultdict(list)
    for ln in out.splitlines():
        m = re.match(r"([\d.]+)\s.*?x86_pkg_temp=(\d+)", ln)
        if m:
            ts = float(m.group(1))
            buckets[int(ts // bucket_s) * bucket_s].append(int(m.group(2)))
    res = []
    for b in sorted(buckets):
        vals = sorted(buckets[b])
        res.append((b + bucket_s / 2, vals[max(0, int(len(vals) * 0.95) - 1)]))
    return res


def resets_per_day(boots, days: int) -> list[tuple[date, int]]:
    """Число жёстких сбросов по дням (включая дни без сбросов)."""
    today = date.today()
    counts = {today - timedelta(days=i): 0 for i in range(days - 1, -1, -1)}
    for b in boots:
        d = b.last.date()
        if not b.clean and d in counts:
            counts[d] += 1
    return list(counts.items())
