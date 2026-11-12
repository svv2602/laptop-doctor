"""Persistent app state (~/.local/state/laptop-doctor/state.json): last auto-clean, reported resets, snoozes."""
from __future__ import annotations

import json
import threading
from pathlib import Path

STATE_DIR = Path.home() / ".local" / "state" / "laptop-doctor"
STATE = STATE_DIR / "state.json"
_lock = threading.Lock()  # пишут и фоновые потоки (проверки, очистка), и окно


def load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def update(**changes) -> dict:
    """Атомарно прочитать, изменить и сохранить состояние."""
    with _lock:
        st = load()
        st.update(changes)
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False))
        tmp.replace(STATE)
        return st
