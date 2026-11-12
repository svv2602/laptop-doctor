"""Projects: unpushed/uncommitted git work and build artifacts in inactive projects."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .util import run

# Только чтение: git не будет брать index.lock (в проектах могут параллельно работать другие сессии)
GIT_ENV = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}


def git(repo: Path, *args: str, timeout: float = 20) -> str | None:
    return run(["git", "-C", str(repo), *args], timeout=timeout, env=GIT_ENV)


def _ignored(p: Path, cfg: Config) -> bool:
    return p.name in cfg.git_ignore or str(p) in {str(Path(x).expanduser()) for x in cfg.git_ignore}


def project_dirs(cfg: Config) -> list[Path]:
    """Все папки проектов: прямые подпапки project_roots; у не-git-папок — ещё и git-репозитории уровнем ниже."""
    res = []
    for root in cfg.project_roots:
        r = Path(root).expanduser()
        if not r.is_dir():
            continue
        for child in sorted(r.iterdir()):
            if not child.is_dir() or child.name.startswith(".") or _ignored(child, cfg):
                continue
            res.append(child)
            if not (child / ".git").exists():
                try:
                    res += [g for g in sorted(child.iterdir())
                            if g.is_dir() and (g / ".git").exists() and not _ignored(g, cfg)]
                except OSError:
                    pass
    return res


def repos(cfg: Config) -> list[Path]:
    return [p for p in project_dirs(cfg) if (p / ".git").exists()]


# ------------------------------------------------------------------ git state

@dataclass
class RepoState:
    path: Path
    branch: str = "?"
    dirty_files: int = 0
    dirty_newest: float = 0.0   # mtime самой свежей незакоммиченной правки
    ahead: int = 0               # коммитов, которых нет на сервере
    ahead_oldest: float = 0.0    # время самого старого из них
    has_remote: bool = True
    has_upstream: bool = True
    last_commit: float = 0.0
    error: str = ""

    @property
    def name(self) -> str:
        return self.path.name


def parse_porcelain_z(out: str) -> list[str]:
    """`git status --porcelain -z` → пути (для переименований — новый путь)."""
    parts = out.split("\0")
    paths, i = [], 0
    while i < len(parts):
        entry = parts[i]
        if len(entry) > 3:
            paths.append(entry[3:])
            if entry[0] in "RC":  # за переименованием следует старый путь — пропускаем
                i += 1
        i += 1
    return paths


def repo_state(repo: Path) -> RepoState:
    st = RepoState(repo)
    status = git(repo, "status", "--porcelain", "-z", "--untracked-files=normal")
    if status is None:
        st.error = "git status не выполнился"
        return st
    paths = parse_porcelain_z(status)
    st.dirty_files = len(paths)
    for rel in paths:
        try:
            st.dirty_newest = max(st.dirty_newest, (repo / rel).lstat().st_mtime)
        except OSError:  # удалённый файл — берём время папки
            try:
                st.dirty_newest = max(st.dirty_newest, (repo / rel).parent.stat().st_mtime)
            except OSError:
                pass
    st.branch = (git(repo, "rev-parse", "--abbrev-ref", "HEAD") or "?").strip()
    st.last_commit = float((git(repo, "log", "-1", "--format=%ct") or "0").strip() or 0)
    st.has_remote = bool((git(repo, "remote") or "").strip())
    upstream = git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    st.has_upstream = upstream is not None
    if st.has_upstream:
        rng = ["@{u}..HEAD"]
    elif st.has_remote:
        rng = ["HEAD", "--not", "--remotes"]  # ветка без upstream: коммиты, которых нет ни в одной удалённой ветке
    else:
        rng = []
    if rng:
        times = (git(repo, "log", "--format=%ct", *rng) or "").split()
        st.ahead = len(times)
        st.ahead_oldest = float(times[-1]) if times else 0.0
    return st


def hours_ago(ts: float) -> float:
    return (time.time() - ts) / 3600 if ts else 0.0


def fmt_age(ts: float) -> str:
    h = hours_ago(ts)
    if h < 1:
        return f"{h * 60:.0f} мин"
    if h < 48:
        return f"{h:.0f} ч"
    return f"{h / 24:.0f} дн"


# ------------------------------------------------------------------ build artifacts

@dataclass
class Artifact:
    path: Path
    kind: str


@dataclass
class ProjectArtifacts:
    project: Path
    last_activity: float
    artifacts: list[Artifact] = field(default_factory=list)


def _artifacts_in(d: Path) -> list[Artifact]:
    """Мусор сборки прямо в папке d — только при наличии файла, подтверждающего тип проекта."""
    res = []

    def add(rel: str, kind: str, marker: str | None):
        p = d / rel
        if p.is_dir() and not p.is_symlink() and (marker is None or (d / marker).exists()):
            res.append(Artifact(p, kind))

    add("node_modules", "node_modules", "package.json")
    add(".next", ".next", "package.json")
    add("target", "target (Rust)", "Cargo.toml")
    add(".dart_tool", ".dart_tool", "pubspec.yaml")
    add("build", "build (Flutter)", "pubspec.yaml")
    add("android/.gradle", "android/.gradle", "pubspec.yaml")
    for venv in (".venv", "venv"):
        if (d / venv / "pyvenv.cfg").exists():
            add(venv, venv, None)
    return res


def last_activity(p: Path) -> float:
    """Когда проект последний раз трогали: последний коммит, свежие правки, файлы верхнего уровня."""
    times = [0.0]
    if (p / ".git").exists():
        times.append(float((git(p, "log", "-1", "--format=%ct") or "0").strip() or 0))
        times.append((p / ".git" / "index").stat().st_mtime if (p / ".git" / "index").exists() else 0)
    skip = {"node_modules", ".venv", "venv", "target", ".dart_tool", "build", ".next", ".git", ".idea"}
    try:
        for e in p.iterdir():
            if e.name not in skip:
                try:
                    times.append(e.lstat().st_mtime)
                except OSError:
                    pass
    except OSError:
        pass
    return max(times)


def running_compose_dirs() -> list[Path]:
    """Папки compose-проектов, у которых сейчас работают контейнеры."""
    out = run(["docker", "ps", "--format", '{{.Label "com.docker.compose.project.working_dir"}}'], timeout=20) or ""
    return [Path(x).resolve() for x in set(out.split()) if x]


def inactive_artifacts(cfg: Config) -> list[ProjectArtifacts]:
    cutoff = time.time() - cfg.dev_artifacts_min_age_days * 86400
    busy = running_compose_dirs()
    res, seen = [], set()
    for p in project_dirs(cfg):
        rp = p.resolve()
        if any(rp == b or rp in b.parents or b in rp.parents for b in busy):
            continue  # у проекта работают контейнеры — он в деле, не трогаем
        act = last_activity(p)
        if act >= cutoff:
            continue
        arts = _artifacts_in(p)
        try:  # монорепо: frontend/, backend/ …
            for sub in p.iterdir():
                if sub.is_dir() and not sub.name.startswith(".") and sub.name not in ("node_modules", "target", "build"):
                    arts += _artifacts_in(sub)
        except OSError:
            pass
        arts = [a for a in arts if a.path.resolve() not in seen]
        seen.update(a.path.resolve() for a in arts)
        if arts:
            res.append(ProjectArtifacts(p, act, arts))
    return res


def is_inside_home(p: Path) -> bool:
    home = Path(os.path.expanduser("~")).resolve()
    rp = p.resolve()
    return rp != home and home in rp.parents
