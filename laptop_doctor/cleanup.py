"""Cleanup targets. estimate() is read-only; plan() describes what clean would do."""
from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Config
from .util import dir_size, parse_docker_size, run

HOME = Path.home()
ANON_VOLUME = re.compile(r"^[0-9a-f]{64}$")


@dataclass
class Plan:
    root_actions: list[dict] = field(default_factory=list)
    user_cmds: list[list[str]] = field(default_factory=list)
    # Пути (внутри $HOME), которые удаляются целиком или очищаются по содержимому
    remove_paths: list[Path] = field(default_factory=list)
    empty_paths: list[Path] = field(default_factory=list)


@dataclass
class Target:
    id: str
    title: str
    note: str
    needs_root: bool
    estimate: Callable[[], tuple[int, str]]  # (байт, пояснение)
    plan: Callable[[], Plan]
    default_checked: bool = True


# ------------------------------------------------------------------ docker

def _docker_dangling() -> tuple[int, str]:
    out = run(["docker", "images", "-f", "dangling=true", "--format", "{{.Size}}"], timeout=20)
    if out is None:
        return 0, "Docker недоступен"
    sizes = [parse_docker_size(s) for s in out.split()]
    return sum(sizes), f"{len(sizes)} образ(ов) без тега"


def _docker_build_cache() -> tuple[int, str]:
    out = run(["docker", "builder", "du"], timeout=30) or ""
    m = re.search(r"Reclaimable:\s*(\S+)", out)
    return (parse_docker_size(m.group(1)) if m else 0), "кэш docker build"


def docker_anon_volumes() -> list[tuple[str, int]]:
    out = run(["docker", "system", "df", "-v", "--format", "{{json .}}"], timeout=120)
    if not out:
        return []
    data = json.loads(out)
    vols = []
    for v in data.get("Volumes") or []:
        if ANON_VOLUME.match(v.get("Name", "")) and int(v.get("Links", 1)) == 0:
            vols.append((v["Name"], parse_docker_size(v.get("Size", "0B"))))
    return vols


# Классификация содержимого тома — внутри временного контейнера (только чтение, без сети).
# Пользователь в группе docker, поэтому root не нужен.
PROBE_IMAGES = ("alpine:latest", "busybox:latest", "redis:7-alpine", "postgres:16-alpine")
PROBE_SCRIPT = r'''
for v in "$@"; do d=/v/$v/_data
  if [ -z "$(ls -A "$d" 2>/dev/null)" ]; then t=empty
  elif [ -e "$d/PG_VERSION" ] || [ -e "$d/ibdata1" ] || [ -e "$d/aria_log_control" ] || [ -e "$d/mysql" ] \
       || [ -e "$d/dump.rdb" ] || [ -e "$d/appendonlydir" ] || [ -e "$d/chroma.sqlite3" ]; then t=data
  elif [ -e "$d/.bin" ] || [ -e "$d/.package-lock.json" ] || [ -e "$d/.modules.yaml" ] \
       || ls "$d" | grep -q '^@'; then t=node_modules
  elif [ -e "$d/build-manifest.json" ]; then t=next_cache
  else t=other; fi
  echo "$v $t"
done
'''
JUNK_KINDS = ("empty", "node_modules", "next_cache")


def probe_image() -> str | None:
    for img in PROBE_IMAGES:
        if run(["docker", "image", "inspect", img], timeout=10) is not None:
            return img
    return None


def classify_volumes(names: list[str]) -> dict[str, str]:
    img = probe_image()
    if not names or not img:
        return {}
    out = run(["docker", "run", "--rm", "--network", "none", "-v", "/var/lib/docker/volumes:/v:ro",
               "--entrypoint", "sh", img, "-c", PROBE_SCRIPT, "probe", *names], timeout=120) or ""
    return dict(line.split(maxsplit=1) for line in out.splitlines() if " " in line)


def junk_volumes() -> tuple[list[tuple[str, int]], dict[str, int], bool]:
    """(мусорные тома с размерами, счётчик оставленных по типу, удалось ли классифицировать)."""
    vols = docker_anon_volumes()
    kinds = classify_volumes([n for n, _ in vols])
    if vols and not kinds:
        return [], {}, False
    junk = [(n, s) for n, s in vols if kinds.get(n) in JUNK_KINDS]
    kept: dict[str, int] = {}
    for n, _ in vols:
        k = kinds.get(n, "unknown")
        if k not in JUNK_KINDS:
            kept[k] = kept.get(k, 0) + 1
    return junk, kept, True


KIND_RU = {"data": "с данными БД", "other": "прочих", "unknown": "неизвестных"}


def _docker_volumes() -> tuple[int, str]:
    junk, kept, ok = junk_volumes()
    if not ok:
        return 0, ("не удалось заглянуть в тома: нет локального образа "
                   f"{' / '.join(PROBE_IMAGES)} — скачайте alpine: docker pull alpine")
    keep = ", ".join(f"{n} {KIND_RU.get(k, k)}" for k, n in kept.items())
    return sum(s for _, s in junk), f"{len(junk)} мусорных томов" + (f"; не трогаю: {keep}" if keep else "")


def _docker_volumes_plan() -> Plan:
    junk, _, _ = junk_volumes()  # пересчёт прямо перед удалением
    names = [n for n, _ in junk]
    # docker сам откажет удалять том, если он успел подключиться к контейнеру
    return Plan(user_cmds=[["docker", "volume", "rm", *names]] if names else [])


# ------------------------------------------------------------------ system

def old_snap_revisions() -> list[tuple[str, str, int]]:
    out = run(["snap", "list", "--all"], timeout=20) or ""
    res = []
    for line in out.splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 6 and "disabled" in cols[-1]:
            name, rev = cols[0], cols[2]
            f = Path(f"/var/lib/snapd/snaps/{name}_{rev}.snap")
            res.append((name, rev, f.stat().st_size if f.exists() else 0))
    return res


def _snaps() -> tuple[int, str]:
    revs = old_snap_revisions()
    return sum(s for *_, s in revs), f"{len(revs)} старых версий snap-пакетов"


def autoremove_packages() -> list[str]:
    out = run(["apt-get", "-s", "autoremove"], timeout=60) or ""
    return [line.split()[1] for line in out.splitlines() if line.startswith("Remv ")]


def _apt() -> tuple[int, str]:
    pkgs = autoremove_packages()
    if not pkgs:
        return 0, "нечего удалять"
    out = run(["dpkg-query", "-W", "-f=${Installed-Size}\n", *pkgs], timeout=20) or ""
    kb = sum(int(x) for x in out.split() if x.isdigit())
    return kb * 1024, f"{len(pkgs)} пакет(ов): " + ", ".join(pkgs[:6]) + ("…" if len(pkgs) > 6 else "")


def _crash() -> tuple[int, str]:
    files = list(Path("/var/crash").glob("*.crash"))
    return sum(f.stat().st_size for f in files if f.exists()), f"{len(files)} отчёт(ов) об аварийном завершении"


# ------------------------------------------------------------------ user caches

def _dir_target(path: Path) -> Callable[[], tuple[int, str]]:
    return lambda: (dir_size(path), str(path).replace(str(HOME), "~"))


def make_targets(cfg: Config) -> list[Target]:
    trash = HOME / ".local/share/Trash"
    user_dirs = [
        ("pip", "Кэш pip", HOME / ".cache/pip", "пакеты скачаются заново при установке"),
        ("uv", "Кэш uv", HOME / ".cache/uv", "пакеты скачаются заново при установке"),
        ("npm", "Кэш npm", HOME / ".npm/_cacache", "пакеты скачаются заново при установке"),
        ("yarn", "Кэш yarn", HOME / ".cache/yarn", "пакеты скачаются заново при установке"),
        ("thumbs", "Миниатюры файлов", HOME / ".cache/thumbnails", "пересоздадутся при просмотре папок"),
        ("gradle", "Кэш Gradle", HOME / ".gradle/caches", "следующая сборка Android будет долгой — всё скачается заново"),
    ]
    targets = [
        Target("docker_dangling", "Docker: образы без тега", "старые слои после пересборок", False,
               _docker_dangling, lambda: Plan(user_cmds=[["docker", "image", "prune", "-f"]])),
        Target("docker_cache", "Docker: кэш сборки", "следующая сборка образа будет дольше", False,
               _docker_build_cache, lambda: Plan(user_cmds=[["docker", "builder", "prune", "-f"]])),
        Target("docker_volumes", "Docker: мусорные тома", "node_modules и кэши сборки от старых запусков", False,
               _docker_volumes, _docker_volumes_plan),
        Target("snaps", "Старые версии snap", "snap хранит предыдущие версии для отката", True,
               _snaps, lambda: Plan(root_actions=[{"action": "remove-snap-revisions",
                                                   "revisions": [[n, r] for n, r, _ in old_snap_revisions()]}])),
        Target("apt", "Ненужные пакеты (старые ядра и т.п.)", "apt autoremove --purge", True,
               _apt, lambda: Plan(root_actions=[{"action": "apt-autoremove"}]), default_checked=False),
        Target("crash", "Аварийные отчёты", "/var/crash/*.crash", True,
               _crash, lambda: Plan(root_actions=[{"action": "clean-crash"}])),
        Target("trash", "Корзина", "файлы из корзины KDE удалятся навсегда", False,
               _dir_target(trash), lambda: Plan(empty_paths=[trash / "files", trash / "info", trash / "expunged"]),
               default_checked=False),
    ]
    for tid, title, path, note, in user_dirs:
        targets.append(Target(tid, title, note, False, _dir_target(path),
                              (lambda p=path: Plan(empty_paths=[p])), default_checked=(tid != "gradle")))

    def dev_estimate() -> tuple[int, str]:
        from . import projects
        from .util import dir_sizes, human
        found = projects.inactive_artifacts(cfg)
        sizes = dir_sizes([a.path for pa in found for a in pa.artifacts])
        per = sorted(((sum(sizes.get(a.path, 0) for a in pa.artifacts), pa.project.name) for pa in found), reverse=True)
        total = sum(x for x, _ in per)
        if not cfg.project_roots:
            return 0, "не настроено (project_roots)"
        return total, f"{len(per)} проект(ов): " + ", ".join(f"{n} {human(x)}" for x, n in per[:6]) + \
            ("…" if len(per) > 6 else "")

    def dev_plan() -> Plan:
        from . import projects
        return Plan(remove_paths=[a.path for pa in projects.inactive_artifacts(cfg) for a in pa.artifacts])

    targets.append(Target("dev_artifacts", f"Мусор сборки в проектах, не менявшихся {cfg.dev_artifacts_min_age_days}+ дн.",
                          "node_modules, .venv, target/, .dart_tool, build/ — пересоздаются при следующей "
                          "установке/сборке; проекты с работающими контейнерами не трогаются", False,
                          dev_estimate, dev_plan))
    return targets


# ------------------------------------------------------------------ user-side execution

def _safe(p: Path) -> Path:
    rp = p.resolve()
    if rp == HOME or HOME not in rp.parents:
        raise ValueError(f"путь вне домашней папки: {p}")
    return rp


def apply_user_paths(plan: Plan) -> list[str]:
    log = []
    for p in plan.remove_paths:
        rp = _safe(p)
        if rp.exists():
            shutil.rmtree(rp, ignore_errors=True)
            log.append(f"удалено: {rp}")
    for p in plan.empty_paths:
        rp = _safe(p)
        if not rp.is_dir():
            continue
        for child in rp.iterdir():
            try:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
            except OSError as e:
                log.append(f"не удалось: {child}: {e}")
        log.append(f"очищено: {rp}")
    return log


# ------------------------------------------------------------------ плановая очистка

STATE = Path.home() / ".local" / "state" / "laptop-doctor" / "state.json"


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(d: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(d))


def auto_clean_due(cfg: Config, now: float | None = None) -> bool:
    if not cfg.auto_clean_enabled:
        return False
    last = _state().get("last_auto_clean", 0)
    return (now or time.time()) - last >= cfg.auto_clean_interval_days * 86400


def run_auto_clean(cfg: Config) -> tuple[int, list[str]]:
    """Чистит цели из auto_clean_targets, которым не нужен пароль. Возвращает (освобождено байт, лог)."""
    from .actions import run_user
    from .util import human
    chosen = [t for t in make_targets(cfg) if t.id in cfg.auto_clean_targets and not t.needs_root]
    freed, log = 0, []
    for t in chosen:
        size, _ = t.estimate()
        if not size:
            continue
        plan = t.plan()
        if plan.root_actions:  # на всякий случай: без пароля в фоне ничего от root
            continue
        ok, out = run_user(plan.user_cmds) if plan.user_cmds else (True, "")
        log += apply_user_paths(plan)
        if ok:
            freed += size
            log.append(f"✓ {t.title}: {human(size)}")
        else:
            log.append(f"✗ {t.title}: {out[-200:]}")
    st = _state()
    st["last_auto_clean"] = time.time()
    st["last_auto_clean_freed"] = freed
    _save_state(st)
    return freed, log
