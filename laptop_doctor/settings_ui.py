"""Settings dialog: every Config field, grouped into tabs, with Russian labels and hints."""
from __future__ import annotations

import dataclasses
import subprocess
from datetime import datetime

from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from .config import CONFIG_PATH, Config

# поле: (вкладка, подпись, подсказка, (мин, макс) для чисел)
FIELDS: dict[str, tuple[str, str, str, tuple | None]] = {
    "check_interval_min": ("Общее", "Проверка каждые, мин", "", (1, 120)),
    "history_days": ("Общее", "Хранить историю, дней", "графики и события", (7, 365)),
    "fix_applied_since": ("Общее", "Фикс применён (для «до и после»)",
                          "ГГГГ-ММ-ДД ЧЧ:ММ:СС; пусто — без сравнения", None),
    "reset_report_notify_hours": ("Общее", "Уведомлять о сбросах не старше, ч", "старые — только отчёт", (1, 720)),
    "battery": ("Железо", "Батарея", "имя в /sys/class/power_supply", None),
    "battery_threshold": ("Железо", "Ожидаемый порог заряда, %",
                          "программа проверяет, что порог такой; сам порог ставит кнопка в карточке «Батарея»", (50, 100)),
    "auto_kick_charging": ("Железо", "Сам перезапускать зависшую зарядку", "без пароля, не чаще раза в 30 мин", None),
    "dgpu_pci": ("Железо", "PCI-адрес дискретной видеокарты", "должна быть обесточена; пусто — не проверять", None),
    "temp_warn_c": ("Железо", "Температура CPU: предупреждать от, °C", "по p95 за час", (60, 110)),
    "throttle_warn_pct": ("Железо", "Троттлинг: предупреждать от, % времени", "", (1, 100)),
    "kernel_error_ignore": ("Железо", "Ошибки ядра — считать шумом", "регулярки, по одной на строку", None),
    "mem_warn_available_pct": ("Память и тесты", "Память: доступно меньше, %", "предупреждение", (1, 50)),
    "swap_warn_used_pct": ("Память и тесты", "Swap занят больше, %", "предупреждение", (1, 100)),
    "mem_pressure_warn_pct": ("Память и тесты", "Давление памяти больше, %", "предупреждение", (0.5, 100)),
    "mem_critical_available_pct": ("Память и тесты", "Критично: доступно меньше, %", "мгновенное уведомление",
                                   (1, 50)),
    "mem_critical_pressure_pct": ("Память и тесты", "Критично: давление больше, %", "", (1, 100)),
    "mem_watch_interval_s": ("Память и тесты", "Сторож памяти: каждые, с", "", (5, 300)),
    "auto_stop_tests_on_memory_critical": ("Память и тесты", "Сам останавливать тесты при критической нехватке",
                                           "", None),
    "test_max_minutes": ("Память и тесты", "Тест идёт дольше, мин — «завис?»", "", (1, 1440)),
    "test_process_patterns": ("Память и тесты", "Процессы тестов", "регулярки по командной строке", None),
    "test_container_prefixes": ("Память и тесты", "CI-контейнеры: префиксы имени", "", None),
    "test_container_images": ("Память и тесты", "CI-контейнеры: образы", "регулярки", None),
    "docker_cpu_warn_pct": ("Docker и сервисы", "Контейнер грузит CPU больше, %", "", (1, 1600)),
    "docker_cpu_ignore_prefixes": ("Docker и сервисы", "Не считать пожирателями CPU", "префиксы имён", None),
    "abandoned_compose_projects": ("Docker и сервисы", "Заброшенные compose-проекты", "не должны работать", None),
    "stopped_containers": ("Docker и сервисы", "Контейнеры, которые должны быть остановлены", "", None),
    "disabled_units": ("Docker и сервисы", "Отключённые сервисы systemd", "не должны снова включаться", None),
    "journal_warn_mb": ("Docker и сервисы", "Журнал больше, МБ", "", (100, 100000)),
    "boot_userspace_warn_s": ("Docker и сервисы", "Загрузка системы дольше, с", "", (1, 300)),
    "project_roots": ("Проекты", "Папки с проектами", "по одной на строку, например ~/Projects", None),
    "git_dirty_warn_hours": ("Проекты", "Незакоммиченное старше, ч", "", (1, 8760)),
    "git_ignore": ("Проекты", "Не проверять репозитории", "имя папки или путь", None),
    "dev_artifacts_min_age_days": ("Проекты", "Мусор сборки в проектах старше, дн", "", (1, 3650)),
    "disk_warn_free_pct": ("Очистка", "Диск: свободно меньше, %", "предупреждение", (1, 90)),
    "auto_clean_enabled": ("Очистка", "Плановая очистка", "только кэши без пароля", None),
    "auto_clean_interval_days": ("Очистка", "Плановая очистка раз в, дн", "", (1, 90)),
    "auto_clean_targets": ("Очистка", "Что чистить по плану", "pip, uv, npm, yarn, thumbs, docker_dangling…", None),
    "large_files_dirs": ("Очистка", "Где искать крупные файлы", "", None),
    "large_file_min_mb": ("Очистка", "Крупный файл от, МБ", "", (1, 100000)),
    "large_file_min_age_days": ("Очистка", "Не трогали дольше, дн", "", (1, 3650)),
    "ports_allow": ("Безопасность", "Разрешённые открытые порты", "номер или диапазон 1714-1764", None),
}
TABS = ["Общее", "Железо", "Память и тесты", "Docker и сервисы", "Проекты", "Очистка", "Безопасность"]


class SettingsDialog(QDialog):
    def __init__(self, cfg: Config, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Настройки — Здоровье ноутбука")
        self.resize(720, 620)
        self.editors: dict[str, QWidget] = {}
        tabs = QTabWidget()
        forms: dict[str, QFormLayout] = {}
        for name in TABS:
            page = QWidget()
            forms[name] = QFormLayout(page)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            tabs.addTab(scroll, name)
        types = {f.name: f.type for f in dataclasses.fields(Config)}
        for key, (tab, label, hint, rng) in FIELDS.items():
            if key not in types:
                continue
            w = self._editor(key, getattr(cfg, key), rng)
            self.editors[key] = w
            lbl = QLabel(label)
            if hint:
                lbl.setToolTip(hint)
                w.setToolTip(hint)
            box = QWidget()
            lay = QVBoxLayout(box)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(w)
            if hint and not isinstance(w, QCheckBox):
                h = QLabel(hint)
                h.setStyleSheet("color: palette(placeholder-text);")
                h.setWordWrap(True)
                lay.addWidget(h)
            forms[tab].addRow(lbl, box)
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        raw = QPushButton("Открыть файл настроек")
        raw.clicked.connect(lambda: subprocess.Popen(["xdg-open", str(CONFIG_PATH)], start_new_session=True))
        buttons.addButton(raw, QDialogButtonBox.ButtonRole.ResetRole)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    @staticmethod
    def _editor(key: str, value, rng):
        if isinstance(value, bool):
            w = QCheckBox()
            w.setChecked(value)
        elif isinstance(value, int):
            w = QSpinBox()
            w.setRange(int(rng[0]) if rng else 0, int(rng[1]) if rng else 10 ** 6)
            w.setValue(value)
        elif isinstance(value, float):
            w = QDoubleSpinBox()
            w.setDecimals(1)
            w.setRange(float(rng[0]) if rng else 0.0, float(rng[1]) if rng else 1e6)
            w.setValue(value)
        elif isinstance(value, list):
            w = QPlainTextEdit("\n".join(map(str, value)))
            w.setFixedHeight(84)
        else:
            w = QLineEdit(str(value))
        return w

    def values(self) -> dict:
        out = {}
        for key, w in self.editors.items():
            if isinstance(w, QCheckBox):
                out[key] = w.isChecked()
            elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                out[key] = w.value()
            elif isinstance(w, QPlainTextEdit):
                out[key] = [ln.strip() for ln in w.toPlainText().splitlines() if ln.strip()]
            else:
                out[key] = w.text().strip()
        return out

    def _save(self):
        vals = self.values()
        err = validate(vals)
        if err:
            QMessageBox.warning(self, "Настройки", err)
            return
        for k, v in vals.items():
            setattr(self.cfg, k, v)
        self.cfg.save()
        self.accept()


def validate(vals: dict) -> str | None:
    import re
    if vals.get("fix_applied_since"):
        try:
            datetime.fromisoformat(vals["fix_applied_since"])
        except ValueError:
            return "«Фикс применён»: формат ГГГГ-ММ-ДД ЧЧ:ММ:СС, например 2026-11-12 12:31:00"
    for key in ("kernel_error_ignore", "test_process_patterns", "test_container_images"):
        for rx in vals.get(key, []):
            try:
                re.compile(rx)
            except re.error as e:
                return f"Неверная регулярка «{rx}»: {e}"
    for item in vals.get("ports_allow", []):
        if not re.fullmatch(r"\d+(-\d+)?", item):
            return f"Порт «{item}»: нужен номер или диапазон вида 1714-1764"
    if vals.get("battery") and not re.fullmatch(r"BAT\d", vals["battery"]):
        return "Батарея: имя вида BAT0 или BAT1"
    return None
