"""User config: ~/.config/laptop-doctor/config.json (created with defaults on first run)."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "laptop-doctor" / "config.json"


@dataclass
class Config:
    # Проверка интервала в минутах
    check_interval_min: int = 5
    # Момент применения фикса (например, отключения dGPU), "YYYY-MM-DD HH:MM:SS".
    # Сбросы после него считаются отдельно. Пусто — без разделения.
    fix_applied_since: str = ""
    # PCI-адрес дискретной видеокарты, которая должна быть обесточена (например "0000:04:00.0"). Пусто — не проверять.
    dgpu_pci: str = ""
    battery: str = "BAT1"
    battery_threshold: int = 80
    temp_warn_c: int = 90
    # Процессор троттлит по температуре больше N% времени между проверками — предупреждение
    throttle_warn_pct: float = 20.0
    disk_warn_free_pct: int = 15
    # Память: предупреждать, если доступно меньше N% RAM, swap занят больше N%,
    # или процессы ждут память больше N% времени (PSI some avg60)
    mem_warn_available_pct: int = 10
    swap_warn_used_pct: int = 50
    mem_pressure_warn_pct: float = 5.0
    # Критично (быстрый сторож шлёт уведомление сразу): доступно < N% или PSI avg10 >= N%
    mem_critical_available_pct: int = 5
    mem_critical_pressure_pct: float = 20.0
    # Как часто быстрый сторож смотрит на память, секунд (только /proc, почти бесплатно)
    mem_watch_interval_s: int = 15
    # Тесты: процессы (регулярки по командной строке) и контейнеры (префикс имени / регулярка по образу)
    test_process_patterns: list[str] = field(default_factory=lambda: [
        r"phpunit", r"paratest", r"\bpest\b", r"\bpytest\b", r"vitest", r"\bjest\b",
        r"playwright", r"flutter_tester", r"\brspec\b",
    ])
    test_container_prefixes: list[str] = field(default_factory=list)
    test_container_images: list[str] = field(default_factory=list)
    # Тест, идущий дольше N минут, считается подозрительным (завис?)
    test_max_minutes: int = 30
    # Автоматически останавливать тесты при критической нехватке памяти
    auto_stop_tests_on_memory_critical: bool = False
    journal_warn_mb: int = 1024
    boot_userspace_warn_s: float = 15.0
    docker_cpu_warn_pct: float = 25.0
    # Имена контейнеров с этими префиксами не считаем «пожирателями CPU» (временные CI)
    docker_cpu_ignore_prefixes: list[str] = field(default_factory=lambda: ["ci-"])
    # Заброшенные compose-проекты: их контейнеры не должны работать
    abandoned_compose_projects: list[str] = field(default_factory=list)
    # Отдельные контейнеры, которые должны быть остановлены
    stopped_containers: list[str] = field(default_factory=list)
    # Системные юниты, которые мы отключили и которые не должны снова включаться
    disabled_units: list[str] = field(default_factory=list)
    # Папки с проектами (git-репозитории на 1–2 уровня вглубь): незапушенная работа и мусор сборки
    project_roots: list[str] = field(default_factory=list)
    # Незакоммиченные правки старше N часов — предупреждение
    git_dirty_warn_hours: int = 24
    # Репозитории (имя папки или путь), которые не проверять
    git_ignore: list[str] = field(default_factory=list)
    # Мусор сборки (node_modules, .venv, target/…) чистится в проектах, не менявшихся N дней
    dev_artifacts_min_age_days: int = 30
    # Ошибки ядра: регулярки строк, которые считать шумом (известные баги BIOS и т.п.)
    kernel_error_ignore: list[str] = field(default_factory=list)
    # Плановая очистка (только цели без пароля) раз в N дней
    auto_clean_enabled: bool = False
    auto_clean_interval_days: int = 7
    auto_clean_targets: list[str] = field(default_factory=lambda: [
        "pip", "uv", "npm", "yarn", "thumbs", "docker_dangling"])
    # История замеров для графиков, дней
    history_days: int = 30
    # Разбор жёстких сбросов: уведомлять о сбросах не старше N часов (старые — только отчёт)
    reset_report_notify_hours: int = 24
    # Порты: слушающие не только на localhost считаются открытыми наружу; эти порты/диапазоны разрешены
    ports_allow: list[str] = field(default_factory=lambda: ["1714-1764"])  # KDE Connect
    # Автоматически перезапускать зависшую зарядку (без пароля, отдельный узкий помощник)
    auto_kick_charging: bool = False
    # Крупные старые файлы: где искать, от какого размера (МБ) и возраста (дней)
    large_files_dirs: list[str] = field(default_factory=lambda: ["~/Downloads"])
    large_file_min_mb: int = 100
    large_file_min_age_days: int = 60

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text())
                legacy = {"arc_disabled_since": "fix_applied_since", "arc_pci": "dgpu_pci",
                          "flutter_build_min_age_days": "dev_artifacts_min_age_days"}
                for k, v in data.items():
                    if k == "flutter_roots":  # старое имя: папки Flutter-проектов теперь в project_roots
                        cfg.project_roots = list(dict.fromkeys([*cfg.project_roots, *v]))
                        continue
                    k = legacy.get(k, k)
                    if hasattr(cfg, k):
                        setattr(cfg, k, v)
            except (OSError, json.JSONDecodeError):
                pass
        else:
            cfg.save()
        return cfg

    def save(self) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n")
