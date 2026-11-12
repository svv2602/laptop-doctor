"""Health checks. Every check is read-only; fixes are described, not executed, here."""
from __future__ import annotations

import json
import re
import shutil
import statistics
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from pathlib import Path
from typing import Callable

from .config import Config
from .util import read, read_int, run, thermal_zones


class Status(IntEnum):
    OK = 0
    INFO = 1
    WARN = 2
    ERROR = 3


STATUS_LABEL = {Status.OK: "OK", Status.INFO: "Инфо", Status.WARN: "Внимание", Status.ERROR: "Проблема"}


@dataclass
class Fix:
    label: str
    confirm: str
    # Действия от root (выполняет helper через pkexec), список словарей {"action": ..., ...}
    root_actions: list[dict] = field(default_factory=list)
    # Команды от пользователя
    user_cmds: list[list[str]] = field(default_factory=list)
    # Действие внутри программы (выполняется до root-действий), возвращает строки лога
    py_action: Callable[[], list[str]] | None = None


@dataclass
class CheckResult:
    id: str
    title: str
    status: Status
    summary: str
    details: str = ""
    fix: Fix | None = None


# ---------------------------------------------------------------- hard resets

@dataclass
class BootEnd:
    boot_id: str
    first: datetime
    last: datetime
    clean: bool
    last_sample: str  # последняя строка hw-watch в этой загрузке


_boot_cache: dict[str, BootEnd] = {}

CLEAN_MARKERS = ("Journal stopped", "systemd-shutdow", "Reached target poweroff.target",
                 "Reached target reboot.target")


def is_clean_ending(tail: str) -> bool:
    return any(m in tail for m in CLEAN_MARKERS)


def finished_boots() -> list[BootEnd]:
    out = run(["journalctl", "--list-boots", "-o", "json", "--no-pager"])
    if not out:
        return []
    boots = json.loads(out)
    result = []
    for b in boots:
        if b.get("index") == 0:
            continue  # текущая загрузка
        bid = b["boot_id"]
        if bid not in _boot_cache:
            tail = run(["journalctl", "-b", bid, "-n", "10", "-o", "cat", "--no-pager"]) or ""
            sample = run(["journalctl", "-b", bid, "-u", "hw-watch", "-n", "1", "-o", "cat", "--no-pager"]) or ""
            _boot_cache[bid] = BootEnd(
                boot_id=bid,
                first=datetime.fromtimestamp(b["first_entry"] / 1e6),
                last=datetime.fromtimestamp(b["last_entry"] / 1e6),
                clean=is_clean_ending(tail),
                last_sample=sample.strip(),
            )
        result.append(_boot_cache[bid])
    return result


def short_sample(sample: str) -> str:
    keys = ("x86_pkg_temp", "TCPU", "BAT1", "load")
    parts = [p for p in re.split(r"\s+", sample) if p.split("=")[0] in keys]
    return " ".join(parts)


def check_resets(cfg: Config) -> CheckResult:
    boots = finished_boots()
    since = datetime.fromisoformat(cfg.fix_applied_since) if cfg.fix_applied_since else None
    resets = [b for b in boots if not b.clean]
    after = [b for b in resets if since and b.last >= since]
    lines = []
    for b in reversed(resets):
        mark = ("  [ПОСЛЕ фикса]" if b.last >= since else "  [до фикса]") if since else ""
        lines.append(f"{b.last:%d.%m %H:%M:%S}{mark}  {short_sample(b.last_sample) or 'нет данных hw-watch'}")
    oldest = boots[0].first if boots else None
    details = (
        "Жёсткий сброс = загрузка, журнал которой оборвался без штатного выключения.\n"
        f"Журнал хранит историю с {oldest:%d.%m.%Y}.\n\n" if oldest else ""
    ) + ("\n".join(lines) if lines else "Жёстких сбросов в журнале нет.")
    if since is None:
        if resets:
            return CheckResult("resets", "Жёсткие сбросы", Status.WARN,
                               f"{len(resets)} сброс(ов) в журнале — последний {resets[-1].last:%d.%m %H:%M}", details)
        return CheckResult("resets", "Жёсткие сбросы", Status.OK, "Нет сбросов в журнале", details)
    if after:
        return CheckResult("resets", "Жёсткие сбросы", Status.ERROR,
                           f"{len(after)} сброс(ов) после фикса — последний {after[-1].last:%d.%m %H:%M}",
                           details + "\n\nФикс не помог (или причина не одна).")
    days = max((datetime.now() - since).total_seconds() / 86400, 0)
    return CheckResult("resets", "Жёсткие сбросы", Status.OK,
                       f"Нет сбросов после фикса ({days:.1f} дн. наблюдения)", details)


# ---------------------------------------------------------------- discrete GPU

def check_dgpu(cfg: Config) -> CheckResult:
    title = "Дискретная видеокарта"
    if not cfg.dgpu_pci:
        return CheckResult("dgpu", title, Status.INFO, "Не отслеживается (dgpu_pci в настройках пуст)")
    dev = Path("/sys/bus/pci/devices") / cfg.dgpu_pci
    if not dev.exists():
        return CheckResult("dgpu", title, Status.INFO, f"{cfg.dgpu_pci} не найдено на шине PCI")
    state = read(dev / "power_state", "?")
    control = read(dev / "power" / "control", "?")
    driver = (dev / "driver").resolve().name if (dev / "driver").exists() else None
    details = f"PCI {cfg.dgpu_pci}\npower_state={state}\npower/control={control}\nдрайвер={driver or 'не привязан'}"
    if driver:
        return CheckResult("dgpu", title, Status.WARN, f"Драйвер {driver} снова привязан",
                           details + "\n\nПроверьте параметр ядра, отключающий драйвер (например i915.force_probe=!<id>).")
    if state == "D3cold":
        return CheckResult("dgpu", title, Status.OK, "Обесточена (D3cold)", details)
    fix = Fix("Включить автосон видеокарты", "Записать power/control=auto для видеокарты (нужен пароль).",
              root_actions=[{"action": "arc-autosuspend", "pci": cfg.dgpu_pci}])
    return CheckResult("dgpu", title, Status.WARN, f"Не спит: {state}, control={control}", details, fix)


# ---------------------------------------------------------------- temperature

def parse_pkg_temps(log: str) -> list[int]:
    return [int(m) for m in re.findall(r"x86_pkg_temp=(\d+)", log)]


def p95(vals: list[int]) -> int:
    s = sorted(vals)
    return s[max(0, int(len(s) * 0.95) - 1)]


_throttle_prev: tuple[float, int] | None = None  # (uptime, package_throttle_total_time_ms)


def throttle_info() -> tuple[str, float] | None:
    """Троттлинг пакета CPU: текст и доля времени в троттлинге с прошлой проверки (%)."""
    global _throttle_prev
    base = Path("/sys/devices/system/cpu/cpu0/thermal_throttle")
    total_ms, count = read_int(base / "package_throttle_total_time_ms"), read_int(base / "package_throttle_count")
    up = read("/proc/uptime")
    if total_ms is None or not up:
        return None
    uptime = float(up.split()[0])
    since_boot = total_ms / 10 / uptime  # мс → % от времени
    recent, had_prev = 0.0, False
    if _throttle_prev and uptime > _throttle_prev[0]:
        recent = (total_ms - _throttle_prev[1]) / 10 / (uptime - _throttle_prev[0])
        had_prev = True
    _throttle_prev = (uptime, total_ms)
    text = (f"Троттлинг по температуре (процессор сбрасывает частоту): {count} событий с загрузки, "
            f"суммарно {total_ms / 1000:.0f} с — {since_boot:.1f}% времени"
            + (f"; с прошлой проверки {recent:.1f}%" if had_prev else "")
            + ".\nКороткие события по миллисекундам — норма для тонкого ноутбука; плохо, когда это заметная доля времени.")
    return text, recent


def check_temps(cfg: Config) -> CheckResult:
    zones = thermal_zones()
    cur = zones.get("x86_pkg_temp") or zones.get("TCPU")
    def hw(since: str) -> list[int]:
        log = run(["journalctl", "-u", "hw-watch", "--since", since, "-o", "cat", "--no-pager"], timeout=20) or ""
        return parse_pkg_temps(log)

    day, hour = hw("today"), hw("-1h")
    details = "Сейчас: " + ", ".join(f"{k}={v}°C" for k, v in zones.items()) + "\n"
    status = Status.OK
    summary = f"Сейчас {cur}°C" if cur is not None else "Нет данных"
    if hour:
        hp95 = p95(hour)
        summary += f", за час p95 {hp95}°C"
        details += f"За последний час ({len(hour)} замеров): медиана {statistics.median(hour):.0f}°C, p95 {hp95}°C\n"
        # статус — по последнему часу: долгий перегрев, а не разовый пик
        if hp95 >= cfg.temp_warn_c:
            status = Status.WARN
    if day:
        details += (f"За сегодня ({len(day)} замеров): медиана {statistics.median(day):.0f}°C, "
                    f"p95 {p95(day)}°C, максимум {max(day)}°C")
    else:
        details += "hw-watch за сегодня ничего не записал (сервис работает?)"
    details += f"\n\nПорог предупреждения: {cfg.temp_warn_c}°C (95% замеров за час)."
    thr = throttle_info()
    if thr:
        details += "\n\n" + thr[0]
        if thr[1] >= cfg.throttle_warn_pct:
            status = max(status, Status.WARN)
            summary += f", троттлинг {thr[1]:.0f}% времени"
    return CheckResult("temps", "Температура процессора", status, summary, details)


# ---------------------------------------------------------------- battery

def check_battery(cfg: Config) -> CheckResult:
    b = Path("/sys/class/power_supply") / cfg.battery
    if not b.exists():
        return CheckResult("battery", "Батарея", Status.INFO, "Батарея не найдена")
    thr = read_int(b / "charge_control_end_threshold")
    full, design = read_int(b / "charge_full"), read_int(b / "charge_full_design")
    cycles = read_int(b / "cycle_count")
    cap, st = read_int(b / "capacity"), read(b / "status", "?")
    ac = read("/sys/class/power_supply/ADP1/online") == "1"
    health = f"{full / design * 100:.0f}%" if full and design else "?"
    summary = f"{cap}% ({st}), порог {thr}%, износ: осталось {health} ёмкости"
    details = (f"Заряд: {cap}%, статус: {st}, от сети: {'да' if ac else 'нет'}\n"
               f"Порог заряда: {thr}%\nЁмкость: {health} от паспортной\nЦиклов: {cycles}")
    if thr is not None and thr != cfg.battery_threshold:
        fix = Fix(f"Вернуть порог {cfg.battery_threshold}%",
                  f"Установить порог заряда {cfg.battery_threshold}% (нужен пароль).",
                  root_actions=[{"action": "set-battery-threshold", "battery": cfg.battery,
                                 "value": cfg.battery_threshold}])
        return CheckResult("battery", "Батарея", Status.WARN, summary, details, fix)
    return CheckResult("battery", "Батарея", Status.OK, summary, details)


# ---------------------------------------------------------------- docker

def docker_containers() -> list[dict] | None:
    out = run(["docker", "ps", "-a", "--format", "{{json .}}"], timeout=20)
    if out is None:
        return None
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def compose_project(c: dict) -> str:
    m = re.search(r"com\.docker\.compose\.project=([^,]+)", c.get("Labels", ""))
    return m.group(1) if m else ""


def stop_fix(names: list[str]) -> Fix:
    return Fix("Остановить и снять с автозапуска",
               "Остановить контейнеры и поставить им restart=no:\n" + "\n".join(names),
               user_cmds=[["docker", "stop", *names], ["docker", "update", "--restart=no", *names]])


def check_docker_loops(cfg: Config) -> CheckResult:
    cs = docker_containers()
    if cs is None:
        return CheckResult("docker_loops", "Docker: циклы перезапуска", Status.INFO, "Docker недоступен")
    looping = [c for c in cs if c.get("State") == "restarting" or c.get("Status", "").startswith("Restarting")]
    if not looping:
        return CheckResult("docker_loops", "Docker: циклы перезапуска", Status.OK, "Нет контейнеров в цикле перезапуска")
    parts = []
    for c in looping:
        logs = run(["docker", "logs", "--tail", "8", c["Names"]], timeout=10) or ""
        # docker logs пишет stderr контейнера в stderr — берём через run_full
        if not logs:
            from .util import run_full
            _, logs = run_full(["docker", "logs", "--tail", "8", c["Names"]], timeout=10)
        parts.append(f"■ {c['Names']} ({compose_project(c) or 'без проекта'}) — {c['Status']}\n{logs.strip()}")
    names = [c["Names"] for c in looping]
    return CheckResult("docker_loops", "Docker: циклы перезапуска", Status.ERROR,
                       "Перезапускаются: " + ", ".join(names),
                       "\n\n".join(parts) + "\n\nСмотрите последние строки лога: обычно там причина (как было с pyotp у fp_backend).",
                       stop_fix(names))


def check_docker_cpu(cfg: Config) -> CheckResult:
    out = run(["docker", "stats", "--no-stream", "--format", "{{json .}}"], timeout=30)
    if out is None:
        return CheckResult("docker_cpu", "Docker: нагрузка CPU", Status.INFO, "Docker недоступен")
    stats = [json.loads(line) for line in out.splitlines() if line.strip()]
    total = 0.0
    hogs = []
    rows = []
    for s in stats:
        cpu = float(s.get("CPUPerc", "0%").rstrip("%") or 0)
        total += cpu
        rows.append((cpu, s["Name"], s.get("MemUsage", "").split("/")[0].strip()))
        if cpu >= cfg.docker_cpu_warn_pct and not any(s["Name"].startswith(p) for p in cfg.docker_cpu_ignore_prefixes):
            hogs.append(s["Name"])
    rows.sort(reverse=True)
    details = f"Работает контейнеров: {len(stats)}, суммарно CPU {total:.0f}%\n\n" + "\n".join(
        f"{cpu:6.1f}%  {mem:>10}  {name}" for cpu, name, mem in rows[:15])
    if hogs:
        return CheckResult("docker_cpu", "Docker: нагрузка CPU", Status.WARN,
                           "Много CPU: " + ", ".join(hogs), details +
                           "\n\nЗамер разовый: если это сборка или тесты — подождите и обновите.", stop_fix(hogs))
    return CheckResult("docker_cpu", "Docker: нагрузка CPU", Status.OK,
                       f"{len(stats)} контейнеров, суммарно {total:.0f}% CPU", details)


def check_unhealthy(cfg: Config) -> CheckResult:
    title = "Docker: нездоровые контейнеры"
    out = run(["docker", "ps", "--filter", "health=unhealthy", "--format", "{{.Names}}"], timeout=20)
    if out is None:
        return CheckResult("unhealthy", title, Status.INFO, "Docker недоступен")
    names = out.split()
    if not names:
        return CheckResult("unhealthy", title, Status.OK, "Все контейнеры с healthcheck здоровы")
    parts = []
    for n in names:
        h = run(["docker", "inspect", "-f", "{{json .State.Health}}", n], timeout=10) or "{}"
        try:
            health = json.loads(h) or {}
        except json.JSONDecodeError:
            health = {}
        last = (health.get("Log") or [{}])[-1]
        parts.append(f"■ {n}: {health.get('FailingStreak', '?')} неудачных проверок подряд\n"
                     f"  последний ответ (код {last.get('ExitCode', '?')}): {(last.get('Output') or '').strip()[:400]}")
    fix = Fix("Перезапустить", "Перезапустить контейнеры:\n" + "\n".join(names),
              user_cmds=[["docker", "restart", *names]])
    return CheckResult("unhealthy", title, Status.WARN, "Нездоровы: " + ", ".join(names),
                       "\n\n".join(parts) + "\n\nКонтейнер работает, но его healthcheck не проходит — "
                       "обычно сервис внутри завис или не может достучаться до зависимости.", fix)


def check_abandoned(cfg: Config) -> CheckResult:
    if not cfg.abandoned_compose_projects and not cfg.stopped_containers:
        return CheckResult("abandoned", "Docker: заброшенные проекты", Status.INFO,
                           "Не настроено (abandoned_compose_projects / stopped_containers)")
    cs = docker_containers()
    if cs is None:
        return CheckResult("abandoned", "Docker: заброшенные проекты", Status.INFO, "Docker недоступен")
    bad = [c["Names"] for c in cs if c.get("State") in ("running", "restarting") and
           (compose_project(c) in cfg.abandoned_compose_projects or c["Names"] in cfg.stopped_containers)]
    watched = ", ".join(cfg.abandoned_compose_projects + cfg.stopped_containers)
    if bad:
        return CheckResult("abandoned", "Docker: заброшенные проекты", Status.WARN,
                           f"Снова работают: {len(bad)} контейнер(ов)",
                           "Работают:\n" + "\n".join(bad) + f"\n\nОтслеживаются: {watched}\n"
                           "Обычно они возвращаются после `docker compose up` в проекте.", stop_fix(bad))
    return CheckResult("abandoned", "Docker: заброшенные проекты", Status.OK, "Остановлены",
                       f"Отслеживаются: {watched}")


# ---------------------------------------------------------------- systemd

REMMINA_UNIT = "snap.remmina.ssh-agent.service"


# Разовые юниты запуска приложений из меню KDE (app-<имя>@<32 hex>.service): «падают», когда
# программа просто завершилась с ненулевым кодом — это не сервис, чинить нечего.
APP_LAUNCH_UNIT = re.compile(r"^app-.*@[0-9a-f]{32}\.(service|scope)$")


def failed_units(user: bool = False) -> list[str]:
    cmd = ["systemctl"] + (["--user"] if user else []) + ["--failed", "--no-legend", "--plain", "--no-pager"]
    out = run(cmd) or ""
    units = [line.split()[0] for line in out.splitlines() if line.strip()]
    return [u for u in units if not APP_LAUNCH_UNIT.match(u)]


def unit_label(u: str) -> str:
    """'app-snap\\x2duserd\\x2dautostart@autostart.service' -> 'app-snap-userd-autostart@autostart'."""
    u = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), u)
    return u.removesuffix(".service")


def check_failed(cfg: Config) -> CheckResult:
    sys_failed, user_failed = failed_units(), failed_units(user=True)
    if not sys_failed and not user_failed:
        return CheckResult("failed", "Упавшие сервисы", Status.OK, "Нет упавших сервисов")
    parts = []
    for u in sys_failed:
        log = run(["journalctl", "-u", u, "-n", "4", "-o", "cat", "--no-pager"]) or ""
        parts.append(f"■ {unit_label(u)}\n{log.strip()}")
    for u in user_failed:
        log = run(["journalctl", "--user", "-u", u, "-n", "4", "-o", "cat", "--no-pager"]) or ""
        parts.append(f"■ {unit_label(u)} (пользовательский)\n{log.strip()}")
    root_actions: list[dict] = []
    others = [u for u in sys_failed if u != REMMINA_UNIT]
    if REMMINA_UNIT in sys_failed:
        root_actions.append({"action": "fix-remmina-socket"})
    if others:
        root_actions.append({"action": "restart-units", "units": others})
    user_cmds = [["systemctl", "--user", "reset-failed", *user_failed],
                 ["systemctl", "--user", "restart", *user_failed]] if user_failed else []
    labels = [unit_label(u) for u in sys_failed + user_failed]
    fix = Fix("Перезапустить", "Перезапустить упавшие сервисы:\n" + "\n".join(labels) +
              ("\n\nДля Remmina сначала будет удалён залипший файл сокета." if REMMINA_UNIT in sys_failed else ""),
              root_actions=root_actions, user_cmds=user_cmds)
    return CheckResult("failed", "Упавшие сервисы", Status.ERROR,
                       "Упали: " + ", ".join(labels), "\n\n".join(parts), fix)


def check_reenabled(cfg: Config) -> CheckResult:
    if not cfg.disabled_units:
        return CheckResult("reenabled", "Отключённые сервисы", Status.INFO, "Не настроено (disabled_units)")
    back = []
    for u in cfg.disabled_units:
        st = (run(["systemctl", "is-enabled", u], ok_codes=(0, 1, 4)) or "").strip()
        if st == "enabled":
            back.append(u)
    details = "Отслеживаются:\n" + "\n".join(cfg.disabled_units)
    if back:
        fix = Fix("Снова отключить", "Отключить и остановить:\n" + "\n".join(back),
                  root_actions=[{"action": "disable-units", "units": back}])
        return CheckResult("reenabled", "Отключённые сервисы", Status.WARN,
                           "Снова включены: " + ", ".join(back),
                           details + "\n\nОбычно их возвращает обновление пакета.", fix)
    return CheckResult("reenabled", "Отключённые сервисы", Status.OK, "Остаются отключёнными", details)


# ---------------------------------------------------------------- power / boot / disk / journal

def check_power(cfg: Config) -> CheckResult:
    prof = (run(["powerprofilesctl", "get"]) or "?").strip()
    names = {"power-saver": "Энергосбережение", "balanced": "Сбалансированный", "performance": "Производительность"}
    plat = read("/sys/firmware/acpi/platform_profile", "?")
    epp = read("/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference", "?")
    ac = read("/sys/class/power_supply/ADP1/online") == "1"
    return CheckResult("power", "Профиль производительности", Status.INFO,
                       f"{names.get(prof, prof)} ({'от сети' if ac else 'от батареи'})",
                       f"power-profiles-daemon: {prof}\nПрофиль платформы: {plat}\nEPP процессора: {epp}")


def parse_boot_time(out: str) -> tuple[float | None, float | None]:
    def secs(s: str) -> float:
        total = 0.0
        for val, unit in re.findall(r"([\d.]+)(min|ms|s)", s):
            total += float(val) * {"min": 60, "s": 1, "ms": 0.001}[unit]
        return total
    total = re.search(r"=\s*([\dmins. ]+?)\s*$", out.splitlines()[0]) if out else None
    us = re.search(r"\+\s*([\dmins. ]+?)\s*\(userspace\)", out)
    return (secs(total.group(1)) if total else None, secs(us.group(1)) if us else None)


def check_boot(cfg: Config) -> CheckResult:
    out = run(["systemd-analyze"]) or ""
    total, userspace = parse_boot_time(out)
    blame = run(["systemd-analyze", "blame", "--no-pager"]) or ""
    details = out.strip() + "\n\nДольше всех:\n" + "\n".join(blame.splitlines()[:8])
    if total is None:
        return CheckResult("boot", "Время загрузки", Status.INFO, "Загрузка ещё не завершена", details)
    status = Status.WARN if userspace and userspace > cfg.boot_userspace_warn_s else Status.OK
    return CheckResult("boot", "Время загрузки", status,
                       f"{total:.1f} с (система {userspace:.1f} с)", details)


def check_disk(cfg: Config) -> CheckResult:
    u = shutil.disk_usage("/")
    pct = u.free / u.total * 100
    from .util import human
    summary = f"Свободно {human(u.free)} из {human(u.total)} ({pct:.0f}%)"
    status = Status.WARN if pct < cfg.disk_warn_free_pct else Status.OK
    return CheckResult("disk", "Место на диске", status, summary,
                       "Очистка — на вкладке «Очистка»." if status else "")


def parse_meminfo(text: str) -> dict[str, int]:
    """/proc/meminfo -> {ключ: байты}."""
    res = {}
    for line in text.splitlines():
        m = re.match(r"(\w+):\s+(\d+)(?:\s+kB)?", line)
        if m:
            res[m.group(1)] = int(m.group(2)) * (1024 if "kB" in line else 1)
    return res


def parse_psi_avg60(text: str) -> float:
    """/proc/pressure/memory -> some avg60 (% времени, когда задачи ждали память)."""
    m = re.search(r"some avg10=[\d.]+ avg60=([\d.]+)", text)
    return float(m.group(1)) if m else 0.0


def top_memory(n: int = 10) -> list[tuple[int, str, int]]:
    """Приложения по суммарному RSS всех их процессов: (байты, имя, число процессов)."""
    out = run(["ps", "-eo", "rss=,comm="]) or ""
    agg: dict[str, list[int]] = {}
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            a = agg.setdefault(parts[1].strip(), [0, 0])
            a[0] += int(parts[0]) * 1024
            a[1] += 1
    return sorted(((v[0], k, v[1]) for k, v in agg.items()), reverse=True)[:n]


def oom_kills(days: int = 7) -> list[str]:
    log = run(["journalctl", "-k", "--since", f"-{days}d", "-o", "short", "--no-pager"], timeout=30) or ""
    kills = [line for line in log.splitlines() if re.search(r"Out of memory: Killed process|oom-kill:", line)]
    oomd = run(["journalctl", "-u", "systemd-oomd", "--since", f"-{days}d", "-o", "short", "--no-pager"], timeout=20) or ""
    kills += [line for line in oomd.splitlines() if "Killed" in line]
    return kills


def check_memory(cfg: Config) -> CheckResult:
    from .util import human
    mi = parse_meminfo(read("/proc/meminfo", "") or "")
    total, avail = mi.get("MemTotal", 0), mi.get("MemAvailable", 0)
    if not total:
        return CheckResult("memory", "Память и swap", Status.INFO, "Нет данных /proc/meminfo")
    used = total - avail
    avail_pct = avail / total * 100
    sw_total, sw_free = mi.get("SwapTotal", 0), mi.get("SwapFree", 0)
    sw_used = sw_total - sw_free
    sw_pct = sw_used / sw_total * 100 if sw_total else 0.0
    psi = parse_psi_avg60(read("/proc/pressure/memory", "") or "")
    kills = oom_kills()

    summary = f"Занято {human(used)} из {human(total)}"
    summary += f", swap {human(sw_used)} ({sw_pct:.0f}%)" if sw_total else ", swap нет"
    problems = []
    if avail_pct < cfg.mem_warn_available_pct:
        problems.append(f"свободной памяти меньше {cfg.mem_warn_available_pct}%")
    if sw_total and sw_pct >= cfg.swap_warn_used_pct:
        problems.append(f"swap занят на {sw_pct:.0f}%")
    if psi >= cfg.mem_pressure_warn_pct:
        problems.append(f"задачи ждут память {psi:.1f}% времени")
    if kills:
        problems.append(f"убито из-за нехватки памяти: {len(kills)} за 7 дней")

    details = (f"Память: занято {human(used)}, доступно {human(avail)} ({avail_pct:.0f}%) из {human(total)}\n"
               f"  кэш файлов {human(mi.get('Cached', 0))} — не входит в «занято», ядро отдаёт его по требованию\n")
    if sw_total:
        details += f"Swap: занято {human(sw_used)} из {human(sw_total)} ({sw_pct:.0f}%)"
        if mi.get("Zswapped"):
            details += f", сжато в zswap {human(mi['Zswapped'])}"
        details += "\n"
    else:
        details += "Swap: не настроен\n"
    details += (f"Давление памяти (PSI, 60 с): {psi:.1f}% — доля времени, когда процессы ждали память.\n"
                "Это лучший признак реальной нехватки: занятый swap сам по себе не страшен.\n\n")
    details += ("Больше всех занимают (RSS всех процессов приложения; у многопроцессных, как браузер, "
                "сумма завышена — общие страницы считаются в каждом процессе):\n") + "\n".join(
        f"{human(b):>10}  {name}" + (f" ({n} проц.)" if n > 1 else "") for b, name, n in top_memory())
    if kills:
        details += "\n\nУбийства из-за нехватки памяти:\n" + "\n".join(kills[-8:])

    from . import testguard
    snap = testguard.mem_snapshot()
    fix = None
    if problems:
        procs, conts = testguard.find_test_processes(cfg), testguard.find_test_containers(cfg)
        if procs or conts:
            fix = tests_fix(procs, conts, snap)
            details += "\n\nЗапущены тесты — возможно, это они:\n" + testguard.describe(procs, conts)
    if fix is None and testguard.swap_refresh_wanted(snap):
        fix = Fix("Вернуть swap в память",
                  f"В swap {human(sw_used)}, а в памяти достаточно места. Вернуть данные из swap в память, "
                  "чтобы программы перестали подтормаживать? (нужен пароль, может занять минуту)",
                  root_actions=[{"action": "swap-refresh"}])
    if problems:
        status = Status.ERROR if kills or psi >= cfg.mem_pressure_warn_pct * 3 else Status.WARN
        return CheckResult("memory", "Память и swap", status, summary + " — " + "; ".join(problems), details, fix)
    return CheckResult("memory", "Память и swap", Status.OK, summary, details, fix)


def tests_fix(procs, conts, snap) -> Fix:
    from . import testguard
    swap = testguard.swap_refresh_wanted_after(snap, procs, conts)
    what = testguard.describe(procs, conts)
    return Fix("Остановить тесты и освободить память",
               "Будут остановлены:\n" + what +
               ("\n\nЗатем данные из swap вернутся в память, чтобы система перестала тормозить "
                "(нужен пароль, может занять минуту)." if swap else ""),
               root_actions=[{"action": "swap-refresh"}] if swap else [],
               py_action=lambda: testguard.stop_tests(procs, conts))


def check_tests(cfg: Config) -> CheckResult:
    from . import testguard
    from .util import human
    title = "Тесты"
    procs, conts = testguard.find_test_processes(cfg), testguard.find_test_containers(cfg)
    snap = testguard.mem_snapshot()
    watched = ("Ищу процессы: " + ", ".join(cfg.test_process_patterns) +
               "\nКонтейнеры: префиксы " + (", ".join(cfg.test_container_prefixes) or "—") +
               "; образы " + (", ".join(cfg.test_container_images) or "—"))
    if not procs and not conts:
        return CheckResult("tests", title, Status.OK, "Не запущены", watched)
    total = sum(p.rss for p in procs) + sum(c.mem for c in conts)
    limit = cfg.test_max_minutes * 60
    stuck = [p for p in procs if p.age_s > limit] + [c for c in conts if c.age_s > limit]
    low = testguard.memory_low(snap, cfg)
    summary = f"Запущено: {len(conts)} контейнер(ов), {len(procs)} процесс(ов), {human(total)} памяти"
    details = testguard.describe(procs, conts) + "\n\n" + watched
    fix = tests_fix(procs, conts, snap)
    if low:
        return CheckResult("tests", title, Status.ERROR, summary + " — не хватает памяти", details, fix)
    if stuck:
        return CheckResult("tests", title, Status.WARN,
                           summary + f" — {len(stuck)} идут дольше {cfg.test_max_minutes} мин (зависли?)", details, fix)
    return CheckResult("tests", title, Status.INFO, summary, details, fix)


def check_git(cfg: Config) -> CheckResult:
    from . import projects
    title = "Незапушенная работа"
    if not cfg.project_roots:
        return CheckResult("git", title, Status.INFO, "Не настроено (project_roots)")
    states = [projects.repo_state(r) for r in projects.repos(cfg)]
    lim = cfg.git_dirty_warn_hours
    stale = [s for s in states if s.dirty_files and projects.hours_ago(s.dirty_newest) > lim]
    fresh = [s for s in states if s.dirty_files and s not in stale]
    unpushed = [s for s in states if s.ahead]
    old_unpushed = [s for s in unpushed if projects.hours_ago(s.ahead_oldest) > lim]
    no_remote = [s for s in states if not s.has_remote and (s.last_commit or s.dirty_files)]
    broken = [s for s in states if s.error]

    def line(s, extra=""):
        return f"  {s.name:<26} ветка {s.branch:<20} {extra}"

    parts = []
    if stale:
        parts.append(f"Не закоммичено давно (> {lim} ч) — риск потерять при сбое:\n" + "\n".join(
            line(s, f"{s.dirty_files} файл(ов), последняя правка {projects.fmt_age(s.dirty_newest)} назад")
            for s in sorted(stale, key=lambda s: s.dirty_newest)))
    if unpushed:
        parts.append("Коммиты не отправлены на сервер:\n" + "\n".join(
            line(s, f"{s.ahead} коммит(ов), самый старый {projects.fmt_age(s.ahead_oldest)} назад"
                    + ("" if s.has_upstream else " (у ветки нет upstream)")) for s in unpushed))
    if fresh:
        parts.append("Текущая работа (свежие правки):\n" + "\n".join(
            line(s, f"{s.dirty_files} файл(ов), {projects.fmt_age(s.dirty_newest)} назад") for s in fresh))
    if no_remote:
        parts.append("Нет удалённого репозитория — код только на этом диске:\n" + "\n".join(
            line(s, "без коммитов" if not s.last_commit else "") for s in no_remote))
    if broken:
        parts.append("Не удалось проверить:\n" + "\n".join(line(s, s.error) for s in broken))
    details = "\n\n".join(parts) or "Всё закоммичено и отправлено."
    details += f"\n\nПроверено репозиториев: {len(states)} в " + ", ".join(cfg.project_roots)
    bits = []
    if stale:
        bits.append(f"давно не закоммичено: {len(stale)}")
    if old_unpushed:
        bits.append(f"не отправлено: {len(old_unpushed)}")
    if no_remote:
        bits.append(f"без remote: {len(no_remote)}")
    if stale or old_unpushed:
        return CheckResult("git", title, Status.WARN, "; ".join(bits), details)
    if fresh or unpushed or no_remote:
        return CheckResult("git", title, Status.INFO,
                           "; ".join(bits) or f"текущая работа в {len(fresh) + len(unpushed)} репо", details)
    return CheckResult("git", title, Status.OK, f"Всё закоммичено и отправлено ({len(states)} репо)", details)


UDISKS = "org.freedesktop.UDisks2"
NVME_IFACE = "org.freedesktop.UDisks2.NVMe.Controller"
_ssd_prev: dict[str, dict] = {}  # прошлые значения счётчиков — чтобы видеть рост между проверками


def _busctl_json(*args: str):
    out = run(["busctl", "--system", "--json=short", *args], timeout=15)
    return json.loads(out)["data"] if out else None


def nvme_drives() -> list[str]:
    out = run(["busctl", "--system", "--list", "tree", UDISKS], timeout=15) or ""
    drives = [x.strip() for x in out.split() if "/drives/" in x]
    res = []
    for d in drives:
        intro = run(["busctl", "--system", "introspect", UDISKS, d], timeout=15) or ""
        if NVME_IFACE in intro:
            res.append(d)
    return res


def nvme_smart(drive: str) -> dict:
    data = _busctl_json("call", UDISKS, drive, NVME_IFACE, "SmartGetAttributes", "a{sv}", "0")
    attrs = {k: v["data"] for k, v in (data[0] if data else {}).items()}
    for prop in ("SmartCriticalWarning", "SmartTemperature", "SmartPowerOnHours"):
        v = _busctl_json("get-property", UDISKS, drive, NVME_IFACE, prop)
        if v is not None:
            attrs[prop] = v
    model = _busctl_json("get-property", UDISKS, drive, "org.freedesktop.UDisks2.Drive", "Model")
    attrs["model"] = model or drive.rsplit("/", 1)[-1]
    return attrs


def check_ssd(cfg: Config) -> CheckResult:
    from .util import human
    title = "Здоровье SSD"
    drives = nvme_drives()
    if not drives:
        return CheckResult("ssd", title, Status.INFO, "NVMe-дисков не найдено (или нет UDisks2)")
    worst, summaries, parts = Status.OK, [], []
    for d in drives:
        a = nvme_smart(d)
        if "percent_used" not in a:
            parts.append(f"{a['model']}: SMART недоступен")
            continue
        st, notes = Status.OK, []
        warn = a.get("SmartCriticalWarning") or []
        if warn:
            st = Status.ERROR
            notes.append("критическое предупреждение диска: " + ", ".join(warn))
        used, spare, thr = a["percent_used"], a.get("avail_spare", 100), a.get("spare_thresh", 10)
        if used >= 100 or spare < thr:
            st = Status.ERROR
            notes.append("ресурс исчерпан — пора менять диск")
        elif used >= 80:
            st = max(st, Status.WARN)
            notes.append(f"износ {used}%")
        if a.get("media_errors", 0):
            st = max(st, Status.WARN)
            notes.append(f"ошибки носителя: {a['media_errors']}")
        prev = _ssd_prev.get(d, {})
        grew = {k: a.get(k, 0) - prev[k] for k in ("critical_temp_time", "media_errors", "unsafe_shutdowns")
                if k in prev and a.get(k, 0) > prev[k]}
        if grew.get("critical_temp_time"):
            st = max(st, Status.WARN)
            notes.append(f"перегрев до критической температуры: +{grew['critical_temp_time']} мин")
        _ssd_prev[d] = {k: a.get(k, 0) for k in ("critical_temp_time", "media_errors", "unsafe_shutdowns")}
        temp_c = a.get("SmartTemperature", 0) - 273 if a.get("SmartTemperature") else None
        worst = max(worst, st)
        summaries.append(f"износ {used}%, {temp_c}°C" + (" — " + "; ".join(notes) if notes else ""))
        parts.append(
            f"{a['model']}\n"
            f"  Износ (percent_used): {used}%   Резерв: {spare}% (порог {thr}%)\n"
            f"  Ошибки носителя: {a.get('media_errors', 0)}   Записей в журнале ошибок: {a.get('num_err_log_entries', 0)}\n"
            f"  Температура: {temp_c}°C (предупреждение {a.get('wctemp', 0) - 273}°C, "
            f"критическая {a.get('cctemp', 0) - 273}°C)\n"
            f"  Всего выше порога предупреждения: {a.get('warning_temp_time', 0)} мин, "
            f"выше критического: {a.get('critical_temp_time', 0)} мин\n"
            f"  Наработка: {a.get('SmartPowerOnHours', 0)} ч, включений: {a.get('power_cycles', 0)}\n"
            f"  Небезопасных выключений: {a.get('unsafe_shutdowns', 0)} — каждое пропадание питания/жёсткий сброс\n"
            f"  Записано: {human(a.get('total_data_written', 0))}, прочитано: {human(a.get('total_data_read', 0))}"
            + ("\n  Внимание: " + "; ".join(notes) if notes else ""))
    return CheckResult("ssd", title, worst, " | ".join(summaries) or "нет данных", "\n\n".join(parts))


# Ошибки, которые всегда серьёзны: железо, диск, файловая система, зависания GPU
SERIOUS_KERNEL = re.compile(
    r"Hardware Error|mce: |Machine check|nvme.*(timeout|reset|I/O error)|PCIe Bus Error|AER:.*(Uncorrected|Fatal)|"
    r"EXT4-fs error|BTRFS (error|critical)|I/O error, dev|GPU HANG|gpu hang|\*ERROR\* .*(reset|hang)|watchdog: BUG",
    re.I)
BOOT_PHASE_S = 120  # ошибки первых двух минут — инициализация (кривые таблицы ACPI и т.п.)


def normalize_kernel_msg(msg: str) -> str:
    msg = re.sub(r"\(\d{8}/[\w-]+-\d+\)", "", msg)        # (20251212/psparse-543)
    msg = re.sub(r"0x[0-9a-fA-F]+|\b[0-9a-f]{12,}\b|\b\d+\b", "N", msg)
    return msg.strip()


def parse_kernel_errors(out: str) -> list[tuple[float, str]]:
    res = []
    for line in out.splitlines():
        m = re.match(r"\[\s*([\d.]+)\]\s+\S+\s+kernel:\s*(.*)", line)
        if m:
            res.append((float(m.group(1)), m.group(2)))
    return res


def check_kernel(cfg: Config) -> CheckResult:
    title = "Ошибки ядра"
    out = run(["journalctl", "-k", "-b", "-p", "err", "-o", "short-monotonic", "--no-pager"], timeout=30)
    if out is None:
        return CheckResult(title, title, Status.INFO, "Журнал ядра недоступен")
    errs = parse_kernel_errors(out)
    ignore = [re.compile(x) for x in cfg.kernel_error_ignore]
    serious = [(t, m) for t, m in errs if SERIOUS_KERNEL.search(m)]
    runtime = [(t, m) for t, m in errs if t > BOOT_PHASE_S and (t, m) not in serious
               and not any(r.search(m) for r in ignore)]
    ignored = [(t, m) for t, m in errs if t > BOOT_PHASE_S and any(r.search(m) for r in ignore)]
    boot = [(t, m) for t, m in errs if t <= BOOT_PHASE_S and (t, m) not in serious]

    def group(items, limit=12):
        g: dict[str, list[float]] = {}
        for t, m in items:
            g.setdefault(normalize_kernel_msg(m), []).append(t)
        rows = sorted(g.items(), key=lambda kv: -len(kv[1]))[:limit]
        return "\n".join(f"  {len(ts):>4}×  {msg[:150]}" for msg, ts in rows)

    parts = []
    if serious:
        parts.append("СЕРЬЁЗНЫЕ (железо/диск/GPU):\n" + group(serious))
    if runtime:
        parts.append("Во время работы:\n" + group(runtime))
    if ignored:
        parts.append(f"Известный шум (kernel_error_ignore), во время работы: {len(ignored)}\n" + group(ignored, 5))
    if boot:
        parts.append(f"При загрузке (первые {BOOT_PHASE_S} с; обычно кривые таблицы ACPI в BIOS — безвредно): "
                     f"{len(boot)}\n" + group(boot, 6))
    details = "\n\n".join(parts) or "Ошибок ядра с момента загрузки нет."
    details += ("\n\nПовторяющийся безвредный шум можно добавить в kernel_error_ignore (регулярки) в настройках."
                if runtime else "")
    if serious:
        return CheckResult("kernel", title, Status.ERROR, f"Серьёзных ошибок: {len(serious)}", details)
    if runtime:
        return CheckResult("kernel", title, Status.WARN, f"Во время работы: {len(runtime)} ошибок", details)
    return CheckResult("kernel", title, Status.OK,
                       f"Нет новых ошибок (при загрузке {len(boot)}, шум {len(ignored)})", details)


def parse_journal_mb(out: str) -> float | None:
    m = re.search(r"take up ([\d.]+)([KMGT])", out)
    if not m:
        return None
    return float(m.group(1)) * {"K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}[m.group(2)]


def check_journal(cfg: Config) -> CheckResult:
    mb = parse_journal_mb(run(["journalctl", "--disk-usage"]) or "")
    if mb is None:
        return CheckResult("journal", "Журнал systemd", Status.INFO, "Размер неизвестен")
    if mb > cfg.journal_warn_mb:
        fix = Fix("Ограничить 500 МБ", "Задать SystemMaxUse=500M и обрезать журнал (нужен пароль).",
                  root_actions=[{"action": "journal-limit"}])
        return CheckResult("journal", "Журнал systemd", Status.WARN, f"{mb:.0f} МБ — больше лимита", "", fix)
    return CheckResult("journal", "Журнал systemd", Status.OK, f"{mb:.0f} МБ")


_updates_cache: tuple[float, dict] | None = None
UPDATES_TTL_S = 6 * 3600  # snap и fwupd ходят в сеть — не чаще раза в 6 часов


def collect_updates() -> dict:
    res: dict = {"apt": None, "apt_security": None, "snap": [], "fw": [], "flatpak": None}
    from .util import run_full
    code, out = run_full(["/usr/lib/update-notifier/apt-check"], timeout=60)  # печатает "всего;безопасности" в stderr
    m = re.search(r"(\d+);(\d+)", out)
    if m:
        res["apt"], res["apt_security"] = int(m.group(1)), int(m.group(2))
    else:
        lst = run(["apt", "list", "--upgradable"], timeout=60) or ""
        res["apt"] = sum(1 for ln in lst.splitlines() if "/" in ln)
    snaps = run(["snap", "refresh", "--list"], timeout=60) or ""
    res["snap"] = [ln.split()[0] for ln in snaps.splitlines()[1:] if ln.strip()]
    fw = run(["fwupdmgr", "get-updates", "--json", "--no-unreported-check", "--no-metadata-check"], timeout=60,
             ok_codes=(0, 2))
    try:
        res["fw"] = [f"{d.get('Name')}: {d.get('Version')} → {(d.get('Releases') or [{}])[0].get('Version')}"
                     for d in json.loads(fw or "{}").get("Devices", [])]
    except json.JSONDecodeError:
        pass
    if shutil.which("flatpak"):
        fp = run(["flatpak", "remote-ls", "--updates", "--columns=application"], timeout=60)
        res["flatpak"] = len([x for x in (fp or "").splitlines() if x.strip()]) if fp is not None else None
    return res


def open_discover() -> list[str]:
    import subprocess
    subprocess.Popen(["plasma-discover", "--mode", "update"], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return ["✓ открыт Discover — обновления там"]


def check_updates(cfg: Config) -> CheckResult:
    import time
    global _updates_cache
    title = "Обновления"
    if _updates_cache is None or time.time() - _updates_cache[0] > UPDATES_TTL_S:
        _updates_cache = (time.time(), collect_updates())
    checked, u = _updates_cache
    apt, sec, snaps, fw, fp = u["apt"], u["apt_security"], u["snap"], u["fw"], u["flatpak"]
    total = (apt or 0) + len(snaps) + len(fw) + (fp or 0)
    details = (f"Пакеты apt: {apt if apt is not None else '?'}"
               + (f", из них безопасности: {sec}" if sec is not None else "") + "\n"
               f"Snap: {len(snaps)}" + (f" ({', '.join(snaps[:12])})" if snaps else "") + "\n"
               + (f"Flatpak: {fp}\n" if fp is not None else "")
               + "Прошивки (fwupd): " + ("; ".join(fw) if fw else "нет") + "\n"
               + f"\nПроверено в {datetime.fromtimestamp(checked):%H:%M}, повторно — через 6 часов.")
    fix = Fix("Открыть Discover", "Открыть центр программ Discover на странице обновлений?",
              py_action=open_discover) if total and shutil.which("plasma-discover") else None
    parts = []
    if sec:
        parts.append(f"{sec} обновлений безопасности")
    if apt:
        parts.append(f"apt {apt}")
    if snaps:
        parts.append(f"snap {len(snaps)}")
    if fw:
        parts.append(f"прошивки {len(fw)}")
    if fp:
        parts.append(f"flatpak {fp}")
    if sec or fw:
        return CheckResult("updates", title, Status.WARN, ", ".join(parts), details, fix)
    if total:
        return CheckResult("updates", title, Status.INFO, ", ".join(parts), details, fix)
    return CheckResult("updates", title, Status.OK, "Всё обновлено", details)


def check_reboot_required(cfg: Config) -> CheckResult:
    if Path("/run/reboot-required").exists():
        pkgs = read("/run/reboot-required.pkgs", "") or ""
        return CheckResult("reboot", "Перезагрузка после обновлений", Status.INFO, "Нужна перезагрузка",
                           "Пакеты:\n" + pkgs)
    return CheckResult("reboot", "Перезагрузка после обновлений", Status.OK, "Не требуется")


CHECKS: list[Callable[[Config], CheckResult]] = [
    check_resets, check_dgpu, check_temps, check_battery,
    check_docker_loops, check_unhealthy, check_docker_cpu, check_abandoned,
    check_failed, check_reenabled,
    check_memory, check_tests, check_git, check_ssd, check_kernel, check_disk, check_journal, check_boot, check_power, check_updates, check_reboot_required,
]


def run_all(cfg: Config) -> list[CheckResult]:
    results = []
    for fn in CHECKS:
        try:
            results.append(fn(cfg))
        except Exception:  # одна сломанная проверка не должна ронять остальные
            name = fn.__name__.removeprefix("check_")
            results.append(CheckResult(name, name, Status.WARN, "Проверка упала", traceback.format_exc()))
    return results
