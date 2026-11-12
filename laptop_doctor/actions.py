"""Execute fixes and cleanup plans: user commands directly, root actions via pkexec + helper."""
from __future__ import annotations

import json
from pathlib import Path

from .util import run_full

HELPER = Path("/usr/local/libexec/laptop-doctor-helper")


def run_root(actions: list[dict]) -> tuple[bool, str]:
    if not actions:
        return True, ""
    if not HELPER.exists():
        return False, f"Не установлен {HELPER} — запустите install.sh из папки проекта."
    code, out = run_full(["pkexec", str(HELPER), json.dumps(actions, ensure_ascii=False)], timeout=3600)
    if code in (126, 127) and not out.startswith(("✓", "✗")):
        return False, "Отменено: пароль не введён." if code == 126 else out
    return code == 0, out


def run_user(cmds: list[list[str]]) -> tuple[bool, str]:
    ok, log = True, []
    for cmd in cmds:
        code, out = run_full(cmd)
        log.append(("✓ " if code == 0 else "✗ ") + " ".join(cmd) + (f"\n  {out[-400:]}" if code and out else ""))
        ok &= code == 0
    return ok, "\n".join(log)


def execute(root_actions: list[dict], user_cmds: list[list[str]], extra=None) -> tuple[bool, str]:
    """Run user commands, then an optional in-process callable, then root actions (one password prompt)."""
    ok, parts = True, []
    if user_cmds:
        o, out = run_user(user_cmds)
        ok &= o
        parts.append(out)
    if extra is not None:
        try:
            parts.extend(extra())
        except Exception as e:  # noqa: BLE001
            ok = False
            parts.append(f"✗ {e}")
    if root_actions:
        o, out = run_root(root_actions)
        ok &= o
        parts.append(out)
    return ok, "\n".join(p for p in parts if p)
