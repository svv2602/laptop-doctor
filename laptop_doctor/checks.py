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


def check_reboot_required(cfg: Config) -> CheckResult:
    if Path("/run/reboot-required").exists():
        pkgs = read("/run/reboot-required.pkgs", "") or ""
        return CheckResult("reboot", "Перезагрузка после обновлений", Status.INFO, "Нужна перезагрузка",
                           "Пакеты:\n" + pkgs)
    return CheckResult("reboot", "Перезагрузка после обновлений", Status.OK, "Не требуется")


CHECKS: list[Callable[[Config], CheckResult]] = [
    check_resets, check_dgpu, check_temps, check_battery,
    check_docker_loops, check_docker_cpu, check_abandoned,
    check_failed, check_reenabled,
    check_memory, check_tests, check_disk, check_journal, check_boot, check_power, check_reboot_required,
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
