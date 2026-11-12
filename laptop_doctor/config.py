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
    # Где искать Flutter-проекты для чистки build/
    flutter_roots: list[str] = field(default_factory=lambda: ["~/FlutterProjects"])
    flutter_build_min_age_days: int = 30

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text())
                legacy = {"arc_disabled_since": "fix_applied_since", "arc_pci": "dgpu_pci"}
                for k, v in data.items():
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
