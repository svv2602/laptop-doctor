"""Snoozed problems: hidden from the tray colour and notifications until a date or until the summary changes."""
from __future__ import annotations

import time

from . import state


def all_snoozes() -> dict:
    return state.load().get("snoozes", {})


def is_snoozed(check_id: str, summary: str, snoozes: dict | None = None, now: float | None = None) -> bool:
    s = (snoozes if snoozes is not None else all_snoozes()).get(check_id)
    if not s:
        return False
    if s.get("until") and (now or time.time()) > s["until"]:
        return False
    if s.get("summary") is not None and s["summary"] != summary:
        return False  # проблема изменилась — снова показываем
    return True


def snooze(check_id: str, days: float | None = None, summary: str | None = None) -> None:
    snoozes = all_snoozes()
    snoozes[check_id] = {"until": time.time() + days * 86400 if days else None, "summary": summary}
    state.update(snoozes=snoozes)


def unsnooze(check_id: str) -> None:
    snoozes = all_snoozes()
    snoozes.pop(check_id, None)
    state.update(snoozes=snoozes)


def describe(check_id: str, snoozes: dict | None = None) -> str:
    s = (snoozes if snoozes is not None else all_snoozes()).get(check_id) or {}
    if s.get("until"):
        return f"отложено до {time.strftime('%d.%m %H:%M', time.localtime(s['until']))}"
    if s.get("summary") is not None:
        return "скрыто, пока проблема не изменится"
    return ""
