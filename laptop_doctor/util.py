"""Small helpers shared by checks and cleanup: running commands, reading sysfs, formatting."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def run(cmd: list[str], timeout: float = 30, ok_codes: tuple[int, ...] = (0,)) -> str | None:
    """Run a command, return stdout or None on failure/timeout/missing binary."""
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C.UTF-8"},
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
        return None
    if p.returncode not in ok_codes:
        return None
    return p.stdout


def run_full(cmd: list[str], timeout: float = 600) -> tuple[int, str]:
    """Run a command, return (returncode, combined output). Never raises."""
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C.UTF-8"},
        )
    except FileNotFoundError:
        return 127, f"не найдено: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"таймаут: {' '.join(cmd)}"
    return p.returncode, (p.stdout + p.stderr).strip()


def read(path: str | Path, default: str | None = None) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def read_int(path: str | Path) -> int | None:
    v = read(path)
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def thermal_zones() -> dict[str, int]:
    """type -> temperature in °C (first zone of each type wins)."""
    out: dict[str, int] = {}
    for z in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        t = read(z / "type")
        v = read_int(z / "temp")
        if t and v is not None and t not in out:
            out[t] = v // 1000
    return out


def human(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024 or unit == "ТБ":
            return f"{n:.0f} {unit}" if unit in ("Б", "КБ") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


_DOCKER_UNITS = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12,
                 "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4}


def parse_docker_size(s: str) -> int:
    """'1.207GB' / '584MB' / '0B' / '12.3kB' -> bytes."""
    s = s.strip().lower()
    num = ""
    for ch in s:
        if ch.isdigit() or ch == ".":
            num += ch
        else:
            break
    unit = s[len(num):].strip() or "b"
    try:
        return int(float(num) * _DOCKER_UNITS.get(unit, 1))
    except ValueError:
        return 0


def dir_size(path: Path, timeout: float = 300) -> int:
    """Size of a directory in bytes via du (fast, handles permissions). 0 if missing."""
    if not path.exists():
        return 0
    out = run(["du", "-sb", "--", str(path)], timeout=timeout, ok_codes=(0, 1))
    if not out:
        return 0
    try:
        return int(out.split()[0])
    except (ValueError, IndexError):
        return 0
