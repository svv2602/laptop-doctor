"""History for charts: own samples (memory/swap/disk) + journal-derived series (temperature, battery, resets)."""
from __future__ import annotations

import json
import re
import shutil
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from .util import read, run, thermal_zones

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
        "disk_free_gb": round(shutil.disk_usage("/").free / 1024 ** 3, 1),
    }
    ssd = nvme_temp()
    if ssd is not None:
        row["ssd_temp"] = ssd
    thr = read(Path("/sys/devices/system/cpu/cpu0/thermal_throttle/package_throttle_total_time_ms"))
    up = read("/proc/uptime")
    if thr and thr.isdigit() and up:
        row["throttle_ms"], row["uptime"] = int(thr), float(up.split()[0])
    bat = next(iter(sorted(Path("/sys/class/power_supply").glob("BAT*"))), None)
    if bat:
        cap = read(bat / "capacity")
        row["battery_pct"] = int(cap) if cap and cap.isdigit() else None
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a") as f:
        f.write(json.dumps(row) + "\n")


def nvme_temp() -> int | None:
    """Температура NVMe-диска из hwmon (без root)."""
    for h in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        if read(h / "name") == "nvme":
            v = read(h / "temp1_input")
            if v and v.lstrip("-").isdigit():
                return int(v) // 1000
    return None


def throttle_series(rows: list[dict]) -> list[tuple[float, float]]:
    """% времени в троттлинге между соседними замерами (только в пределах одной загрузки)."""
    res = []
    prev = None
    for r in rows:
        if "throttle_ms" not in r:
            prev = None
            continue
        if prev and r["uptime"] > prev["uptime"] and r["throttle_ms"] >= prev["throttle_ms"]:
            dt = r["uptime"] - prev["uptime"]
            res.append((r["ts"], min(100.0, (r["throttle_ms"] - prev["throttle_ms"]) / 10 / dt)))
        prev = r
    return res


# ------------------------------------------------------------------ события для отметок на графиках

EVENTS = STATE_DIR / "events.jsonl"


def log_event(kind: str, text: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with EVENTS.open("a") as f:
        f.write(json.dumps({"ts": int(time.time()), "kind": kind, "text": text}, ensure_ascii=False) + "\n")


def load_events(since_ts: float, boots=()) -> list[tuple[float, str, str]]:
    """Свои события + жёсткие сбросы из журнала загрузок."""
    res = []
    if EVENTS.exists():
        for ln in EVENTS.read_text().splitlines():
            try:
                e = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if e.get("ts", 0) >= since_ts:
                res.append((e["ts"], e.get("kind", ""), e.get("text", "")))
    for b in boots:
        ts = b.last.timestamp()
        if not b.clean and ts >= since_ts:
            res.append((ts, "reset", "жёсткий сброс (журнал оборвался)"))
    return sorted(res)


def prune(days: int) -> None:
    cutoff = time.time() - days * 86400
    for path in (HISTORY, EVENTS):
        if path.exists():
            rows = [ln for ln in path.read_text().splitlines() if ln.strip() and json.loads(ln).get("ts", 0) >= cutoff]
            path.write_text("".join(r + "\n" for r in rows))


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


def hw_watch_series(since_ts: float, bucket_s: int) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Из журнала hw-watch: (температура CPU — p95 по корзине, заряд батареи — последнее значение в корзине)."""
    since = datetime.fromtimestamp(since_ts).strftime("%Y-%m-%d %H:%M:%S")
    out = run(["journalctl", "-u", "hw-watch", "--since", since, "-o", "short-unix", "--no-pager"], timeout=60) or ""
    temps: dict[int, list[int]] = defaultdict(list)
    bats: dict[int, int] = {}
    for ln in out.splitlines():
        m = re.match(r"([\d.]+)\s", ln)
        if not m:
            continue
        b = int(float(m.group(1)) // bucket_s) * bucket_s
        t = re.search(r"x86_pkg_temp=(\d+)", ln)
        if t:
            temps[b].append(int(t.group(1)))
        bat = re.search(r"BAT\d=[^:\s]+:(\d+)%", ln)
        if bat:
            bats[b] = int(bat.group(1))
    temp_series = []
    for b in sorted(temps):
        vals = sorted(temps[b])
        temp_series.append((b + bucket_s / 2, vals[max(0, int(len(vals) * 0.95) - 1)]))
    bat_series = [(b + bucket_s / 2, v) for b, v in sorted(bats.items())]
    return temp_series, bat_series


def resets_per_day(boots, days: int) -> list[tuple[date, int]]:
    """Число жёстких сбросов по дням (включая дни без сбросов)."""
    today = date.today()
    counts = {today - timedelta(days=i): 0 for i in range(days - 1, -1, -1)}
    for b in boots:
        d = b.last.date()
        if not b.clean and d in counts:
            counts[d] += 1
    return list(counts.items())


# ------------------------------------------------------------------ сравнение «до и после фикса»

def before_after(fix_ts: float, boots) -> dict | None:
    """Медиана часовых p95 температуры и сбросы в сутки до и после fix_ts (по всему журналу)."""
    if not fix_ts:
        return None
    boots = list(boots)
    start = min((b.first.timestamp() for b in boots), default=fix_ts)
    now = time.time()
    temps, _ = hw_watch_series(start, 3600)

    def med(vals):
        vals = sorted(vals)
        return vals[len(vals) // 2] if vals else None

    def per_day(lo, hi):
        n = sum(1 for b in boots if not b.clean and lo <= b.last.timestamp() < hi)
        days = max((hi - lo) / 86400, 1e-9)
        return n, days

    rb, db = per_day(start, fix_ts)
    ra, da = per_day(fix_ts, now)
    return {
        "temp_before": med([v for ts, v in temps if ts < fix_ts]),
        "temp_after": med([v for ts, v in temps if ts >= fix_ts]),
        "resets_before": rb, "days_before": db,
        "resets_after": ra, "days_after": da,
    }


# ------------------------------------------------------------------ данные для диаграмм текущего состояния

def current_snapshot() -> dict:
    from . import checks  # отложенный импорт: checks тяжёлый и сам импортирует history косвенно
    from .util import parse_docker_size
    mi = checks.parse_meminfo(read("/proc/meminfo", "") or "")
    du = shutil.disk_usage("/")
    psi_txt = read("/proc/pressure/memory", "") or ""
    psi = dict(re.findall(r"avg(\d+)=([\d.]+)", psi_txt.splitlines()[0])) if psi_txt else {}
    disk_parts = []
    df = run(["docker", "system", "df", "--format", "{{.Type}}\t{{.Size}}"], timeout=30) or ""
    names = {"Images": "Docker: образы", "Containers": "Docker: контейнеры", "Local Volumes": "Docker: тома",
             "Build Cache": "Docker: кэш сборки"}
    for ln in df.splitlines():
        typ, _, size = ln.partition("\t")
        if typ in names:
            disk_parts.append((names[typ], parse_docker_size(size)))
    snaps = sum(f.stat().st_size for f in Path("/var/lib/snapd/snaps").glob("*.snap") if f.is_file())
    disk_parts.append(("Snap-пакеты", snaps))
    jmb = checks.parse_journal_mb(run(["journalctl", "--disk-usage"]) or "")
    if jmb:
        disk_parts.append(("Журнал systemd", int(jmb * 1024 ** 2)))
    ssd = None
    drives = checks.nvme_drives()
    if drives:
        ssd = checks.nvme_smart(drives[0])
    thr = read(Path("/sys/devices/system/cpu/cpu0/thermal_throttle/package_throttle_total_time_ms"))
    up = read("/proc/uptime")
    return {
        "mem": mi, "top_apps": checks.top_memory(5),
        "psi": {k: float(v) for k, v in psi.items()},
        "disk_total": du.total, "disk_free": du.free, "disk_parts": sorted(disk_parts, key=lambda x: -x[1]),
        "ssd": ssd, "ssd_temp": nvme_temp(),
        "throttle_pct": (int(thr) / 10 / float(up.split()[0])) if thr and thr.isdigit() and up else None,
        "uptime_h": float(up.split()[0]) / 3600 if up else None,
    }
