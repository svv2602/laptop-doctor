"""Find and stop runaway test runs (processes and CI containers); cheap memory snapshot for the watcher."""
from __future__ import annotations

import json
import os
import re
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Config
from .util import human, parse_docker_size, read, run


@dataclass
class TestProc:
    pid: int
    age_s: int
    rss: int
    cmd: str


@dataclass
class TestContainer:
    name: str
    image: str
    age_s: int
    mem: int


@dataclass
class MemSnapshot:
    total: int
    available: int
    swap_total: int
    swap_used: int
    psi_avg10: float
    psi_avg60: float

    @property
    def available_pct(self) -> float:
        return self.available / self.total * 100 if self.total else 100.0


def mem_snapshot() -> MemSnapshot:
    """Только /proc — дёшево, можно вызывать каждые несколько секунд."""
    mi = {}
    for line in (read("/proc/meminfo", "") or "").splitlines():
        m = re.match(r"(\w+):\s+(\d+)", line)
        if m:
            mi[m.group(1)] = int(m.group(2)) * 1024
    psi = read("/proc/pressure/memory", "") or ""
    m = re.search(r"some avg10=([\d.]+) avg60=([\d.]+)", psi)
    return MemSnapshot(
        total=mi.get("MemTotal", 0), available=mi.get("MemAvailable", 0),
        swap_total=mi.get("SwapTotal", 0), swap_used=mi.get("SwapTotal", 0) - mi.get("SwapFree", 0),
        psi_avg10=float(m.group(1)) if m else 0.0, psi_avg60=float(m.group(2)) if m else 0.0,
    )


def memory_critical(s: MemSnapshot, cfg: Config) -> bool:
    return s.available_pct < cfg.mem_critical_available_pct or s.psi_avg10 >= cfg.mem_critical_pressure_pct


def memory_low(s: MemSnapshot, cfg: Config) -> bool:
    return s.available_pct < cfg.mem_warn_available_pct or s.psi_avg60 >= cfg.mem_pressure_warn_pct


# ------------------------------------------------------------------ discovery

def find_test_processes(cfg: Config) -> list[TestProc]:
    if not cfg.test_process_patterns:
        return []
    rx = re.compile("|".join(f"(?:{p})" for p in cfg.test_process_patterns))
    out = run(["ps", "-ww", "-o", "pid=,uid=,etimes=,rss=,args=", "-e"]) or ""
    me, uid = os.getpid(), os.getuid()
    res = []
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5 or not parts[0].isdigit():
            continue
        pid, puid, age, rss, args = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]), parts[4]
        # только свои процессы; сам laptop-doctor и шеллы, в чьей строке просто упомянут шаблон, не трогаем
        if puid != uid or pid == me or "laptop_doctor" in args or args.startswith(("/bin/bash -c", "bash -c", "sh -c")):
            continue
        if rx.search(args):
            res.append(TestProc(pid, age, rss * 1024, args))
    return res


def _age_from_iso(ts: str) -> int:
    ts = re.sub(r"\.(\d{6})\d*", r".\1", ts.replace("Z", "+00:00"))
    try:
        return int((datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds())
    except ValueError:
        return 0


def find_test_containers(cfg: Config) -> list[TestContainer]:
    if not cfg.test_container_prefixes and not cfg.test_container_images:
        return []
    out = run(["docker", "ps", "--format", "{{json .}}"], timeout=20)
    if not out:
        return []
    picked = []
    for line in out.splitlines():
        c = json.loads(line)
        name, image = c.get("Names", ""), c.get("Image", "")
        if any(name.startswith(p) for p in cfg.test_container_prefixes) or \
                any(re.search(p, image) for p in cfg.test_container_images):
            picked.append((name, image))
    if not picked:
        return []
    names = [n for n, _ in picked]
    started = run(["docker", "inspect", "-f", "{{.Name}} {{.State.StartedAt}}", *names], timeout=20) or ""
    ages = {ln.split()[0].lstrip("/"): _age_from_iso(ln.split()[1]) for ln in started.splitlines() if " " in ln}
    stats = run(["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.MemUsage}}", *names], timeout=30) or ""
    mems = {ln.split()[0]: parse_docker_size(ln.split()[1]) for ln in stats.splitlines() if len(ln.split()) > 1}
    return [TestContainer(n, img, ages.get(n, 0), mems.get(n, 0)) for n, img in picked]


def describe(procs: list[TestProc], conts: list[TestContainer]) -> str:
    def age(s: int) -> str:
        return f"{s // 3600}ч {s % 3600 // 60:02d}м" if s >= 3600 else f"{s // 60}м"
    lines = [f"контейнер {c.name:<28} {human(c.mem):>9}  {age(c.age_s):>7}  {c.image}" for c in conts]
    lines += [f"процесс   {p.pid:<28} {human(p.rss):>9}  {age(p.age_s):>7}  {p.cmd[:110]}" for p in procs]
    return "\n".join(lines)


# ------------------------------------------------------------------ stopping

def _alive(pid: int) -> bool:
    stat = read(f"/proc/{pid}/stat")
    # поле состояния после «(имя)»; Z — зомби: уже завершился, ждёт, пока родитель его заберёт
    return bool(stat) and stat.rsplit(")", 1)[-1].split()[0] not in ("Z", "X")


def stop_tests(procs: list[TestProc], conts: list[TestContainer]) -> list[str]:
    """SIGTERM процессам, через 5 с SIGKILL оставшимся; контейнеры — docker kill + rm."""
    log = []
    if conts:
        names = [c.name for c in conts]
        from .util import run_full
        code, out = run_full(["docker", "rm", "-f", *names], timeout=120)
        log.append(("✓ " if code == 0 else "✗ ") + "остановлены контейнеры: " + ", ".join(names) +
                   ("" if code == 0 else f"\n  {out[-300:]}"))
    if procs:
        alive = []
        for p in procs:
            try:
                os.kill(p.pid, signal.SIGTERM)
                alive.append(p)
            except ProcessLookupError:
                pass
            except PermissionError as e:
                log.append(f"✗ {p.pid}: {e}")
        deadline = time.time() + 5
        while alive and time.time() < deadline:
            time.sleep(0.3)
            alive = [p for p in alive if _alive(p.pid)]
        for p in alive:
            try:
                os.kill(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        log.append(f"✓ остановлено процессов: {len(procs)}" + (f" (принудительно: {len(alive)})" if alive else ""))
    return log


def swap_refresh_wanted(s: MemSnapshot) -> bool:
    """Есть смысл вернуть swap в память: занято > 1 ГБ и в RAM есть место с запасом 2 ГБ."""
    return s.swap_used > 1024 ** 3 and s.available > s.swap_used + 2 * 1024 ** 3


def swap_refresh_wanted_after(s: MemSnapshot, procs: list[TestProc], conts: list[TestContainer]) -> bool:
    """То же, но с учётом памяти, которая освободится после остановки тестов."""
    freed = sum(p.rss for p in procs) + sum(c.mem for c in conts)
    return swap_refresh_wanted(MemSnapshot(s.total, s.available + freed, s.swap_total, s.swap_used,
                                           s.psi_avg10, s.psi_avg60))
