"""Export everything into one Markdown file — for a service centre, a forum or your own records."""
from __future__ import annotations

import platform
from datetime import datetime
from pathlib import Path

from .checks import STATUS_LABEL, CheckResult
from .util import read


def system_info() -> list[tuple[str, str]]:
    dmi = Path("/sys/class/dmi/id")
    os_name = next((ln.split("=", 1)[1].strip('"') for ln in (read("/etc/os-release", "") or "").splitlines()
                    if ln.startswith("PRETTY_NAME=")), "?")
    up = read("/proc/uptime")
    cpu = next((ln.split(":", 1)[1].strip() for ln in (read("/proc/cpuinfo", "") or "").splitlines()
                if ln.startswith("model name")), "?")
    return [
        ("Производитель", read(dmi / "sys_vendor", "?")),
        ("Модель", f"{read(dmi / 'product_name', '?')} ({read(dmi / 'product_version', '') or '—'})"),
        ("BIOS", f"{read(dmi / 'bios_version', '?')} от {read(dmi / 'bios_date', '?')}"),
        ("Процессор", cpu),
        ("ОС", os_name),
        ("Ядро", platform.release()),
        ("Работает с загрузки", f"{float(up.split()[0]) / 3600:.1f} ч" if up else "?"),
    ]


def build(results: list[CheckResult], before_after: dict | None = None, last_reset_report: Path | None = None) -> str:
    out = [f"# Отчёт о состоянии ноутбука — {datetime.now():%d.%m.%Y %H:%M}", "",
           "Сформирован laptop-doctor. Внимание: в отчёте могут быть имена ваших проектов, контейнеров и файлов.", "",
           "## Система", ""]
    out += [f"- **{k}:** {v}" for k, v in system_info()]
    out += ["", "## Сводка", "", "| Статус | Проверка | Итог |", "|---|---|---|"]
    for r in sorted(results, key=lambda r: -r.status):
        mark = " (отложено)" if r.snoozed else ""
        out.append(f"| {STATUS_LABEL[r.status]}{mark} | {r.title} | {r.summary.replace('|', '/')} |")
    if before_after:
        ba = before_after
        out += ["", "## До и после фикса", "",
                f"- Температура CPU (медиана часовых p95): {ba['temp_before']} °C → {ba['temp_after']} °C",
                f"- Жёсткие сбросы: {ba['resets_before']} за {ba['days_before']:.1f} дн → "
                f"{ba['resets_after']} за {ba['days_after']:.1f} дн"]
    out += ["", "## Подробности", ""]
    for r in sorted(results, key=lambda r: -r.status):
        out += [f"### {r.title} — {STATUS_LABEL[r.status]}", "", r.summary, ""]
        if r.details.strip():
            out += ["```", r.details.strip(), "```", ""]
    if last_reset_report and last_reset_report.exists():
        out += ["## Последний разбор жёсткого сброса", "", last_reset_report.read_text()]
    return "\n".join(out) + "\n"
