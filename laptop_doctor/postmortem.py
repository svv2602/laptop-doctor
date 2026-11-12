"""Hard-reset post-mortem: what the laptop was doing right before the journal stopped."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

from . import state
from .util import human, run

REPORTS = state.STATE_DIR / "reports"


# ------------------------------------------------------------------ топ процессов (пишется в историю)

def parse_top(out: str, n: int = 5) -> list[list]:
    """Вывод `top -b -n 2` → топ процессов по CPU из второго замера (первый — средний с запуска процесса)."""
    blocks = re.split(r"\n(?=top - )", out.strip())
    block = blocks[-1] if blocks else ""
    lines = block.splitlines()
    try:
        hdr = next(i for i, ln in enumerate(lines) if re.match(r"\s*PID\s+USER", ln))
    except StopIteration:
        return []
    cols = lines[hdr].split()
    i_cpu, i_mem, i_res, i_cmd = cols.index("%CPU"), cols.index("%MEM"), cols.index("RES"), cols.index("COMMAND")
    res = []
    for ln in lines[hdr + 1:]:
        parts = ln.split(None, i_cmd)
        if len(parts) <= i_cmd:
            continue
        try:
            cpu = float(parts[i_cpu].replace(",", "."))
        except ValueError:
            continue
        res.append([parts[i_cmd].strip()[:40], round(cpu, 1), parts[i_res]])
        if len(res) >= n:
            break
    return res


def top_processes(n: int = 5) -> list[list]:
    out = run(["top", "-b", "-n", "2", "-d", "0.5", "-w", "200", "-o", "%CPU"], timeout=15) or ""
    return parse_top(out, n)


# ------------------------------------------------------------------ отчёт о сбросе

def fmt_duration(td: timedelta) -> str:
    mins = int(td.total_seconds() // 60)
    return f"{mins // 60} ч {mins % 60} мин" if mins >= 60 else f"{mins} мин"


def fmt_res(res: str) -> str:
    """Колонка RES из top: килобайты числом или с суффиксом m/g/t."""
    m = re.fullmatch(r"([\d.]+)([kmgt]?)", str(res).strip().lower())
    if not m:
        return str(res)
    mult = {"": 1024, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}[m.group(2)]
    return human(float(m.group(1)) * mult)


def _section(title: str, body: str) -> str:
    body = body.strip() or "(пусто)"
    return f"## {title}\n\n```\n{body}\n```\n"


def build_report(boot, history_rows: list[dict], events: list[tuple[float, str, str]], smart: dict | None) -> str:
    """Markdown-отчёт по одной оборвавшейся загрузке."""
    end = boot.last
    since = (end - timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M:%S")
    hw = run(["journalctl", "-b", boot.boot_id, "-u", "hw-watch", "--since", since, "-o", "short", "--no-pager"],
             timeout=30) or ""
    kern = run(["journalctl", "-b", boot.boot_id, "-k", "-n", "40", "-o", "short-monotonic", "--no-pager"],
               timeout=30) or ""
    warn = run(["journalctl", "-b", boot.boot_id, "-p", "warning", "-n", "30", "-o", "short", "--no-pager"],
               timeout=30) or ""
    tail = run(["journalctl", "-b", boot.boot_id, "-n", "25", "-o", "short", "--no-pager"], timeout=30) or ""
    lines = [
        f"# Жёсткий сброс {end:%d.%m.%Y %H:%M:%S}",
        "",
        f"- Загрузка: `{boot.boot_id}`",
        f"- Работала: с {boot.first:%d.%m %H:%M} до {end:%d.%m %H:%M:%S} ({fmt_duration(end - boot.first)})",
        "- Признак: журнал оборвался без штатного выключения — питание пропало или прошивка сбросила машину.",
    ]
    before = [r for r in history_rows if r["ts"] <= end.timestamp()]
    if before:
        r = before[-1]
        ago = end.timestamp() - r["ts"]
        lines += ["", f"## Последний замер laptop-doctor ({ago / 60:.0f} мин до сброса)", ""]
        for key, label in (("cpu_temp", "Температура CPU, °C"), ("ssd_temp", "Температура SSD, °C"),
                           ("mem_avail_pct", "Доступно памяти, %"), ("swap_used_gb", "Занято swap, ГБ"),
                           ("psi60", "Давление памяти, %"), ("battery_pct", "Заряд батареи, %")):
            if r.get(key) is not None:
                lines.append(f"- {label}: {r[key]}")
        if r.get("top_cpu"):
            lines += ["", "Больше всех грузили процессор:", ""]
            lines += [f"- {name}: {cpu}% CPU, память {fmt_res(res_)}" for name, cpu, res_ in r["top_cpu"]]
    near = [e for e in events if end.timestamp() - 3600 <= e[0] < end.timestamp() - 1 and e[1] != "reset"]
    if near:
        lines += ["", "## События за час до сброса", ""]
        lines += [f"- {datetime.fromtimestamp(ts):%H:%M} — {text}" for ts, _, text in near]
    if smart:
        lines += ["", "## SSD сейчас", "",
                  f"- Небезопасных выключений за жизнь диска: {smart.get('unsafe_shutdowns')}",
                  f"- Ошибки носителя: {smart.get('media_errors')}, износ {smart.get('percent_used')}%"]
    lines += ["", _section("hw-watch за 3 минуты до сброса (температура, батарея, нагрузка)", hw),
              _section("Последние сообщения ядра", kern),
              _section("Последние предупреждения и ошибки", warn),
              _section("Последние строки журнала", tail)]
    return "\n".join(lines)


def report_path(boot) -> Path:
    return REPORTS / f"reset-{boot.last:%Y%m%d-%H%M%S}.md"


def new_resets(boots) -> list:
    reported = set(state.load().get("reported_resets", []))
    return [b for b in boots if not b.clean and b.boot_id not in reported]


def write_reports(boots, history_rows, events, smart) -> list[tuple[object, Path]]:
    """Создать отчёты по ещё не разобранным сбросам. Возвращает [(загрузка, путь)]."""
    done = []
    fresh = new_resets(boots)
    if not fresh:
        return done
    REPORTS.mkdir(parents=True, exist_ok=True)
    for b in fresh:
        path = report_path(b)
        path.write_text(build_report(b, history_rows, events, smart))
        done.append((b, path))
    reported = state.load().get("reported_resets", [])
    state.update(reported_resets=list(dict.fromkeys(reported + [b.boot_id for b, _ in done]))[-200:])
    return done


def list_reports() -> list[Path]:
    return sorted(REPORTS.glob("reset-*.md"), reverse=True) if REPORTS.exists() else []


def summary_line(path: Path) -> str:
    first = path.read_text().splitlines()[0] if path.exists() else ""
    return first.lstrip("# ")
