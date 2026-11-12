"""KDE tray app: status icon + window with checks and cleanup."""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import os
import subprocess
import time

from PyQt6.QtCore import QObject, QPointF, QRectF, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QFontDatabase, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (
    QComboBox, QFileDialog, QGridLayout, QScrollArea,
    QAbstractItemView, QApplication, QCheckBox, QHBoxLayout, QHeaderView, QLabel, QMainWindow,
    QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QSystemTrayIcon, QTableWidget,
    QTableWidgetItem, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import actions, checks, cleanup, history, postmortem, report, snooze, state, testguard
from .charts import ChartSlot, DayBarChart, LineChart, SnapshotPanel
from .checks import STATUS_LABEL, CheckResult, Status
from .config import Config
from .settings_ui import SettingsDialog
from .history import log_event
from .util import human

APP_ID = "laptop-doctor"
COLORS = {Status.OK: "#2e9d57", Status.INFO: "#3a7bd5", Status.WARN: "#e0a100", Status.ERROR: "#d63b3b"}


def status_icon(status: Status, size: int = 64) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(COLORS[status]))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(QRectF(4, 4, size - 8, size - 8))
    pen = QPen(QColor("white"), size * 0.11, cap=Qt.PenCapStyle.RoundCap, join=Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    s = size
    if status in (Status.OK, Status.INFO):
        p.drawPolyline([QPointF(s * .3, s * .52), QPointF(s * .44, s * .66), QPointF(s * .7, s * .37)])
    elif status == Status.WARN:
        p.drawLine(int(s * .5), int(s * .27), int(s * .5), int(s * .56))
        p.drawPoint(int(s * .5), int(s * .72))
    else:
        p.drawLine(int(s * .34), int(s * .34), int(s * .66), int(s * .66))
        p.drawLine(int(s * .66), int(s * .34), int(s * .34), int(s * .66))
    p.end()
    return QIcon(pm)


# ------------------------------------------------------------------ workers

class Worker(QThread):
    done = pyqtSignal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn())
        except Exception as e:  # noqa: BLE001
            self.done.emit(e)


class EstimateWorker(QThread):
    progress = pyqtSignal(str, object, str)  # object: размеры > 2 ГБ не влезают в C++ int

    def __init__(self, targets, parent=None):
        super().__init__(parent)
        self.targets = targets

    def run(self):
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(t.estimate): t for t in self.targets}
            for f in as_completed(futs):
                t = futs[f]
                try:
                    size, note = f.result()
                except Exception as e:  # noqa: BLE001
                    size, note = 0, f"ошибка: {e}"
                self.progress.emit(t.id, size, note)


# ------------------------------------------------------------------ window

class MainWindow(QMainWindow):
    def __init__(self, app: "TrayApp"):
        super().__init__()
        self.app = app
        self.setWindowTitle("Здоровье ноутбука")
        self.setWindowIcon(status_icon(Status.OK))
        self.resize(1100, 680)
        self.results: list[CheckResult] = []
        self.mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)

        tabs = QTabWidget()
        tabs.addTab(self._build_status_tab(), "Состояние")
        tabs.addTab(self._build_cleanup_tab(), "Очистка")
        tabs.addTab(self._build_history_tab(), "История")
        tabs.currentChanged.connect(lambda i: self.load_history() if tabs.widget(i) is self.history_tab else None)
        self.setCentralWidget(tabs)

    # ---- status tab
    def _build_status_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.refresh_btn = QPushButton("Проверить сейчас")
        self.refresh_btn.clicked.connect(self.app.refresh)
        self.updated_lbl = QLabel("—")
        cfg_btn = QPushButton("Настройки…")
        cfg_btn.clicked.connect(self.open_settings)
        export_btn = QPushButton("Экспорт отчёта…")
        export_btn.clicked.connect(self.export_report)
        top.addWidget(self.refresh_btn)
        top.addWidget(self.updated_lbl, 1)
        top.addWidget(export_btn)
        top.addWidget(cfg_btn)
        lay.addLayout(top)

        split = QSplitter()
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Статус", "Проверка", "Итог"])
        self.tree.setRootIsDecorated(False)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.tree.currentItemChanged.connect(self._show_details)
        split.addWidget(self.tree)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.details = QPlainTextEdit(readOnly=True)
        self.details.setFont(self.mono)
        self.fix_btn = QPushButton()
        self.fix_btn.setVisible(False)
        self.fix_btn.clicked.connect(self._run_fix)
        rl.addWidget(self.details, 1)
        rl.addWidget(self.fix_btn)
        # отложить проблему: не красит значок и не присылает уведомления
        snooze_row = QHBoxLayout()
        self.snooze_week = QPushButton("Отложить на 7 дней")
        self.snooze_same = QPushButton("Не показывать, пока не изменится")
        self.unsnooze_btn = QPushButton("Вернуть")
        self.snooze_week.clicked.connect(lambda: self._snooze(days=7))
        self.snooze_same.clicked.connect(lambda: self._snooze(same=True))
        self.unsnooze_btn.clicked.connect(self._unsnooze)
        for b in (self.snooze_week, self.snooze_same, self.unsnooze_btn):
            b.setVisible(False)
            snooze_row.addWidget(b)
        rl.addLayout(snooze_row)
        split.addWidget(right)
        split.setSizes([560, 540])
        lay.addWidget(split, 1)
        return w

    def set_results(self, results: list[CheckResult]):
        cur_id = self.tree.currentItem().data(0, Qt.ItemDataRole.UserRole) if self.tree.currentItem() else None
        self.results = results
        self.tree.clear()
        bold = QFont()
        bold.setBold(True)
        muted = self.palette().color(self.palette().ColorRole.PlaceholderText)
        for r in sorted(results, key=lambda r: (r.snoozed, -r.status)):
            label = "Отложено" if r.snoozed else STATUS_LABEL[r.status]
            it = QTreeWidgetItem([label, r.title, r.summary])
            it.setIcon(0, status_icon(Status.INFO if r.snoozed else r.status, 32))
            it.setData(0, Qt.ItemDataRole.UserRole, r.id)
            if r.snoozed:
                for c in range(3):
                    it.setForeground(c, muted)
            elif r.status >= Status.WARN:
                for c in range(3):
                    it.setFont(c, bold)
            self.tree.addTopLevelItem(it)
            if r.id == cur_id:
                self.tree.setCurrentItem(it)
        for c in range(2):
            self.tree.resizeColumnToContents(c)
        if not self.tree.currentItem() and self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        self.updated_lbl.setText(f"Проверено в {datetime.now():%H:%M:%S}")

    def _current(self) -> CheckResult | None:
        it = self.tree.currentItem()
        if not it:
            return None
        rid = it.data(0, Qt.ItemDataRole.UserRole)
        return next((r for r in self.results if r.id == rid), None)

    def _show_details(self, *_):
        r = self._current()
        if not r:
            self.details.clear()
            self.fix_btn.setVisible(False)
            return
        head = f"{r.title}: {r.summary}"
        if r.snoozed:
            head += f"\n({snooze.describe(r.id)} — значок и уведомления это не учитывают)"
        self.details.setPlainText(f"{head}\n\n{r.details}".rstrip())
        self.fix_btn.setVisible(r.fix is not None)
        problem = r.status >= Status.WARN
        self.snooze_week.setVisible(problem and not r.snoozed)
        self.snooze_same.setVisible(problem and not r.snoozed)
        self.unsnooze_btn.setVisible(r.snoozed)
        if r.fix:
            self.fix_btn.setText(r.fix.label + ("  🔒" if r.fix.root_actions else ""))

    def export_report(self):
        default = Path.home() / f"laptop-doctor-report-{datetime.now():%Y%m%d-%H%M}.md"
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить отчёт", str(default), "Markdown (*.md)")
        if not path:
            return
        cfg = self.app.cfg
        results = self.results

        def build():
            boots = checks.finished_boots()
            fix_ts = datetime.fromisoformat(cfg.fix_applied_since).timestamp() if cfg.fix_applied_since else 0
            reports = postmortem.list_reports()
            text = report.build(results, history.before_after(fix_ts, boots), reports[0] if reports else None)
            Path(path).write_text(text)
            return path

        def done(res):
            if isinstance(res, Exception):
                QMessageBox.warning(self, "Отчёт", f"Не удалось сохранить: {res}")
                return
            QMessageBox.information(self, "Отчёт", f"Сохранён:\n{res}\n\nВ нём могут быть имена ваших проектов "
                                    "и файлов — проверьте перед отправкой.")
            subprocess.Popen(["xdg-open", res], start_new_session=True)

        self._job(build, done)

    def open_settings(self):
        dlg = SettingsDialog(self.app.cfg, self)
        if dlg.exec():
            self.app.apply_config()

    def _snooze(self, days=None, same=False):
        r = self._current()
        if not r:
            return
        snooze.snooze(r.id, days=days, summary=r.summary if same else None)
        self._resnooze()

    def _unsnooze(self):
        r = self._current()
        if r:
            snooze.unsnooze(r.id)
            self._resnooze()

    def _resnooze(self):
        # пересчитать без новой проверки: отметки, значок, подсказку
        self.app.apply_snoozes(self.results)
        self.app.update_tray(self.results)
        self.set_results(self.results)
        self._show_details()

    def _run_fix(self):
        r = self._current()
        if not r or not r.fix:
            return
        if QMessageBox.question(self, r.fix.label, r.fix.confirm + "\n\nВыполнить?") != QMessageBox.StandardButton.Yes:
            return
        fix = r.fix
        self.fix_btn.setEnabled(False)
        self._job(lambda: actions.execute(fix.root_actions, fix.user_cmds, fix.py_action),
                  lambda res, label=fix.label: self._fix_done(res, label))

    def _fix_done(self, res, label=""):
        self.fix_btn.setEnabled(True)
        if not isinstance(res, Exception) and res[0]:
            log_event("fix", f"исправление: {label}")
        self._report(res)
        self.app.refresh()

    # ---- cleanup tab
    def _build_cleanup_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.est_btn = QPushButton("Посчитать")
        self.est_btn.clicked.connect(self.estimate)
        self.clean_btn = QPushButton("Очистить выбранное")
        self.clean_btn.setEnabled(False)
        self.clean_btn.clicked.connect(self._clean)
        self.total_lbl = QLabel("Нажмите «Посчитать» — подсчёт может занять пару минут, окно при этом не блокируется.")
        top.addWidget(self.est_btn)
        top.addWidget(self.clean_btn)
        top.addWidget(self.total_lbl, 1)
        lay.addLayout(top)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Что", "Размер", "Подробно", "Пароль"])
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemChanged.connect(lambda *_: self._update_total())
        lay.addWidget(self.table, 1)

        self.clean_log = QPlainTextEdit(readOnly=True)
        self.clean_log.setFont(self.mono)
        self.clean_log.setMaximumHeight(160)
        self.clean_log.setPlaceholderText("Здесь появится отчёт об очистке")
        lay.addWidget(self.clean_log)

        self.targets = cleanup.make_targets(self.app.cfg)
        self.sizes: dict[str, int] = {}
        self.table.setRowCount(len(self.targets))
        for row, t in enumerate(self.targets):
            name = QTableWidgetItem(t.title)
            name.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            name.setCheckState(Qt.CheckState.Checked if t.default_checked else Qt.CheckState.Unchecked)
            name.setToolTip(t.note)
            self.table.setItem(row, 0, name)
            self.table.setItem(row, 1, QTableWidgetItem("—"))
            self.table.setItem(row, 2, QTableWidgetItem(t.note))
            self.table.setItem(row, 3, QTableWidgetItem("🔒" if t.needs_root else ""))
        return w

    def estimate(self):
        if getattr(self, "_est", None) and self._est.isRunning():
            return
        self.sizes.clear()
        self.est_btn.setEnabled(False)
        self.clean_btn.setEnabled(False)
        for row in range(self.table.rowCount()):
            self.table.item(row, 1).setText("считаю…")
        self.total_lbl.setText("Считаю…")
        self._est = EstimateWorker(self.targets, self)
        self._est.progress.connect(self._est_progress)
        self._est.finished.connect(self._est_done)
        self._est.start()

    def _est_progress(self, tid: str, size: int, note: str):
        row = next(i for i, t in enumerate(self.targets) if t.id == tid)
        self.sizes[tid] = size
        self.table.blockSignals(True)
        self.table.item(row, 1).setText(human(size) if size else "0")
        self.table.item(row, 2).setText(f"{note} — {self.targets[row].note}" if note else self.targets[row].note)
        self.table.blockSignals(False)
        self._update_total()

    def _est_done(self):
        self.est_btn.setEnabled(True)
        self.clean_btn.setEnabled(True)
        self._update_total()

    def _checked(self) -> list[cleanup.Target]:
        return [t for row, t in enumerate(self.targets)
                if self.table.item(row, 0).checkState() == Qt.CheckState.Checked and self.sizes.get(t.id, 0) > 0]

    def _update_total(self):
        if not self.sizes:
            return
        sel = sum(self.sizes.get(t.id, 0) for t in self._checked())
        allsz = sum(self.sizes.values())
        self.total_lbl.setText(f"Выбрано: {human(sel)} из {human(allsz)} возможных")

    def _clean(self):
        sel = self._checked()
        if not sel:
            return
        lines = "\n".join(f"• {t.title} — {human(self.sizes[t.id])}" for t in sel)
        need_pw = any(t.needs_root for t in sel)
        if QMessageBox.question(self, "Очистка", f"Будет удалено:\n{lines}\n\n"
                                f"{'Понадобится пароль администратора (один раз).' if need_pw else ''}\nПродолжить?"
                                ) != QMessageBox.StandardButton.Yes:
            return
        plans = [t.plan() for t in sel]
        root = [a for p in plans for a in p.root_actions]
        user = [c for p in plans for c in p.user_cmds]

        def paths():
            log = []
            for p in plans:
                log += cleanup.apply_user_paths(p)
            return log

        self.clean_btn.setEnabled(False)
        self.est_btn.setEnabled(False)
        self.clean_log.setPlainText("Чищу…")
        self._job(lambda: actions.execute(root, user, paths), self._clean_done)

    def _clean_done(self, res):
        self.est_btn.setEnabled(True)
        if isinstance(res, Exception):
            self.clean_log.setPlainText(f"Ошибка: {res}")
        else:
            ok, log = res
            self.clean_log.setPlainText(("Готово.\n" if ok else "Завершено с ошибками.\n") + log)
            log_event("clean", "очистка диска")
        self.estimate()
        self.app.refresh()

    # ---- history tab
    PERIODS = [("24 часа", 86400, 3600 / 4), ("7 дней", 7 * 86400, 3600), ("30 дней", 30 * 86400, 6 * 3600)]

    def _build_history_tab(self) -> QWidget:
        self.history_tab = w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.period = QComboBox()
        for name, *_ in self.PERIODS:
            self.period.addItem(name)
        self.period.setCurrentIndex(1)
        self.period.currentIndexChanged.connect(self.load_history)
        self.hist_note = QLabel()
        self.hist_note.setWordWrap(True)
        top.addWidget(QLabel("Период:"))
        top.addWidget(self.period)
        top.addWidget(self.hist_note, 1)
        lay.addLayout(top)

        inner = QWidget()
        grid = QGridLayout(inner)
        grid.setSpacing(8)
        self.before_after = QLabel()
        self.before_after.setTextFormat(Qt.TextFormat.RichText)
        self.before_after.setWordWrap(True)
        grid.addWidget(self.before_after, 0, 0, 1, 2)
        self.slots: dict[str, ChartSlot] = {
            "temp": ChartSlot(LineChart("Температура CPU (p95)", "°C", y_min=None), None),
            "throttle": ChartSlot(LineChart("Троттлинг CPU, % времени", "%", fmt="{:.1f}", min_span=5),
                                  SnapshotPanel("Троттлинг CPU сейчас")),
            "ssd": ChartSlot(LineChart("Температура SSD", "°C", y_min=None, min_span=10),
                             SnapshotPanel("SSD сейчас")),
            "resets": ChartSlot(DayBarChart("Жёсткие сбросы за день"), None),
            "mem": ChartSlot(LineChart("Доступно памяти", "%", y_max=100), SnapshotPanel("Память сейчас")),
            "psi": ChartSlot(LineChart("Давление памяти (PSI, 1 мин)", "%", fmt="{:.1f}", min_span=5),
                             SnapshotPanel("Давление памяти сейчас")),
            "swap": ChartSlot(LineChart("Занято swap", " ГБ", fmt="{:.2f}", min_span=1), SnapshotPanel("Swap сейчас")),
            "disk": ChartSlot(LineChart("Свободно на диске", " ГБ", min_span=20), SnapshotPanel("Диск сейчас")),
            "battery": ChartSlot(LineChart("Заряд батареи", "%", y_max=100), None),
            "bat_health": ChartSlot(LineChart("Ёмкость батареи, % от паспортной", "%", fmt="{:.1f}", y_min=None,
                                              min_span=5), SnapshotPanel("Ёмкость батареи сейчас")),
        }
        for i, sl in enumerate(self.slots.values()):
            row, col = 1 + i // 2, i % 2
            grid.addWidget(sl, row, col, 1, 2 if i == len(self.slots) - 1 and i % 2 == 0 else 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(inner)
        lay.addWidget(scroll, 1)
        return w

    def load_history(self, *_):
        name, span, bucket = self.PERIODS[self.period.currentIndex()]
        now = time.time()
        t0 = now - span
        cfg = self.app.cfg

        def collect():
            boots = checks.finished_boots()
            rows = history.load(t0)
            temps, bats = history.hw_watch_series(t0, int(bucket))
            days = max(7, round(span / 86400))
            fix_ts = datetime.fromisoformat(cfg.fix_applied_since).timestamp() if cfg.fix_applied_since else 0
            return {
                "rows": rows, "temps": temps, "bats": bats,
                "resets": history.resets_per_day(boots, days),
                "events": history.load_events(t0, boots),
                "snapshot": history.current_snapshot(),
                "before_after": history.before_after(fix_ts, boots),
            }

        self.hist_note.setText("Загружаю…")
        # смена периода во время загрузки: ответ старого запроса отбрасываем, иначе он перезапишет новый
        self.hist_req = getattr(self, "hist_req", 0) + 1
        req = self.hist_req
        self._job(collect, lambda res: self._history_loaded(res, t0, now, span, bucket, req))

    def _history_loaded(self, res, t0, t1, span, bucket, req=None):
        if req is not None and req != self.hist_req:
            return
        if isinstance(res, Exception):
            self.hist_note.setText(f"Ошибка: {res}")
            return
        rows, ev, snap = res["rows"], res["events"], res["snapshot"]
        sl = self.slots
        jgap = bucket * 2.5
        sl["temp"].show_chart(res["temps"], t0, t1, jgap, ev)
        sl["resets"].chart.set_data(res["resets"])
        sl["battery"].show_chart(res["bats"] or history.series(rows, "battery_pct"), t0, t1,
                                 jgap if res["bats"] else 1800, ev)

        def own(slot, points, fill_snapshot):
            # свой ряд: график, если история покрывает хотя бы пятую часть периода, иначе — текущее состояние
            pts = list(points)
            if len(pts) >= 6 and pts[-1][0] - pts[0][0] >= span * 0.2:
                slot.show_chart(pts, t0, t1, 1800, ev)
            else:
                fill_snapshot(slot.snapshot)
                slot.show_snapshot()

        own(sl["mem"], history.series(rows, "mem_avail_pct"), lambda p: self._snap_mem(p, snap))
        own(sl["psi"], history.series(rows, "psi60"), lambda p: self._snap_psi(p, snap))
        own(sl["swap"], history.series(rows, "swap_used_gb"), lambda p: self._snap_swap(p, snap))
        own(sl["disk"], history.series(rows, "disk_free_gb"), lambda p: self._snap_disk(p, snap))
        own(sl["throttle"], history.throttle_series(rows), lambda p: self._snap_throttle(p, snap))
        own(sl["ssd"], history.series(rows, "ssd_temp"), lambda p: self._snap_ssd(p, snap))
        blog = history.load_battery()
        own(sl["bat_health"], [(r["ts"], r["health_pct"]) for r in blog if r["ts"] >= t0],
            lambda p: self._snap_bat_health(p, blog))
        self.before_after.setText(self._before_after_html(res["before_after"]))
        first = min((r["ts"] for r in rows), default=None)
        note = ("Наведите на график, чтобы увидеть значение; разрывы линий — ноутбук был выключен. "
                "Красная вертикаль — жёсткий сброс, серый пунктир — остановка тестов, очистка, исправление.")
        if first:
            note += (f" Свои замеры (память, swap, диск, SSD, троттлинг) — с {datetime.fromtimestamp(first):%d.%m %H:%M};"
                     " пока их мало, вместо графика показано текущее состояние.")
        self.hist_note.setText(note)

    # диаграммы текущего состояния
    HINT = "Истории за период пока мало — показано текущее состояние."

    def _snap_mem(self, p, snap):
        mi = snap["mem"]
        total, avail = mi.get("MemTotal", 0), mi.get("MemAvailable", 0)
        cache = min(mi.get("Cached", 0) + mi.get("Buffers", 0), avail)
        p.set_data(
            headline=f"Доступно {human(avail)} из {human(total)}",
            parts=[("Программы", total - avail, "series", human(total - avail)),
                   ("Кэш файлов", cache, "series2", human(cache)),
                   ("Свободно", avail - cache, "neutral", human(avail - cache))],
            bars=[(name + (f" ×{n}" if n > 1 else ""), b, human(b)) for b, name, n in snap["top_apps"]],
            bars_title="Больше всех занимают", note=self.HINT)

    def _snap_psi(self, p, snap):
        psi = snap["psi"]
        p.set_data(headline=f"Процессы ждали память {psi.get('60', 0):.1f}% времени",
                   meters=[(f"За {lbl}", psi.get(k, 0) / 100, f"{psi.get(k, 0):.1f}%")
                           for k, lbl in (("10", "10 с"), ("60", "1 мин"), ("300", "5 мин"))],
                   note="0% — нехватки нет, больше 5% — тормоза заметны. " + self.HINT)

    def _snap_swap(self, p, snap):
        mi = snap["mem"]
        tot, used = mi.get("SwapTotal", 0), mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)
        p.set_data(headline=f"Занято {human(used)} из {human(tot)}" if tot else "Swap не настроен",
                   meters=[("Заполнение swap", used / tot if tot else 0, f"{used / tot * 100 if tot else 0:.1f}%")],
                   note=self.HINT)

    def _snap_disk(self, p, snap):
        tot, free = snap["disk_total"], snap["disk_free"]
        p.set_data(headline=f"Свободно {human(free)} из {human(tot)}",
                   meters=[("Занято", (tot - free) / tot, f"{(tot - free) / tot * 100:.0f}%")],
                   bars=[(n, v, human(v)) for n, v in snap["disk_parts"] if v > 0],
                   bars_title="Крупные потребители (быстро считаемые)", note=self.HINT)

    def _snap_throttle(self, p, snap):
        pct = snap["throttle_pct"]
        p.set_data(headline=f"{pct:.1f}% времени с загрузки" if pct is not None else "Нет данных",
                   meters=[("Доля времени в троттлинге", (pct or 0) / 100, f"{pct or 0:.1f}%")],
                   note=f"С загрузки прошло {snap['uptime_h'] or 0:.1f} ч. " + self.HINT)

    def _snap_ssd(self, p, snap):
        a = snap["ssd"] or {}
        temp = snap["ssd_temp"]
        crit = (a.get("cctemp") or 358) - 273
        warn = (a.get("wctemp") or 353) - 273
        bars = []
        if a:
            bars = [(f"Выше {warn}°C", a.get("warning_temp_time", 0), f"{a.get('warning_temp_time', 0)} мин"),
                    (f"Выше {crit}°C", a.get("critical_temp_time", 0), f"{a.get('critical_temp_time', 0)} мин")]
        p.set_data(headline=f"{temp}°C" if temp is not None else "Нет данных",
                   meters=[(f"До критической {crit}°C", (temp or 0) / crit, f"{temp}°C из {crit}°C")],
                   bars=bars, bars_title="Всего за жизнь диска", note=self.HINT)

    def _snap_bat_health(self, p, blog):
        last = blog[-1] if blog else None
        h = last["health_pct"] if last else None
        trend = history.battery_trend(blog)
        p.set_data(headline=f"{h:.0f}% от паспортной ёмкости" if h is not None else "Нет данных",
                   meters=[("Осталось от новой батареи", (h or 0) / 100, f"{h or 0:.1f}%")],
                   note=(f"Тренд: {trend}. " if trend else "Замер раз в день; тренд появится через неделю. ")
                   + (f"Циклов: {last['cycles']}." if last and last.get("cycles") is not None else ""))

    @staticmethod
    def _before_after_html(ba) -> str:
        if not ba:
            return ("<b>До и после фикса</b>: задайте <code>fix_applied_since</code> в настройках, "
                    "чтобы сравнить состояние до и после изменения.")

        def cell(v, fmt):
            return "—" if v is None else fmt.format(v)
        rb = ba["resets_before"] / ba["days_before"] if ba["days_before"] else 0
        ra = ba["resets_after"] / ba["days_after"] if ba["days_after"] else 0
        short = ba["days_after"] < 3
        rows = [
            ("Температура CPU, медиана часовых p95", cell(ba["temp_before"], "{} °C"), cell(ba["temp_after"], "{} °C")),
            ("Жёстких сбросов", f"{ba['resets_before']} за {ba['days_before']:.1f} дн ({rb:.2f}/сут)",
             f"{ba['resets_after']} за {ba['days_after']:.1f} дн ({ra:.2f}/сут)"),
        ]
        html = ("<b>До и после фикса</b><table cellspacing=0 cellpadding=3>"
                "<tr><td></td><td><i>до</i></td><td>&nbsp;&nbsp;</td><td><i>после</i></td></tr>")
        for name, b, a in rows:
            html += f"<tr><td>{name}&nbsp;&nbsp;</td><td>{b}</td><td></td><td><b>{a}</b></td></tr>"
        html += "</table>"
        if short:
            html += ("<small>После фикса прошло меньше 3 дней — по сбросам выводы делать рано: "
                     "раньше они случались примерно раз в несколько дней.</small>")
        return html

    # ---- helpers
    def _job(self, fn, cb):
        w = Worker(fn, self)
        w.done.connect(cb)
        jobs = self.__dict__.setdefault("_jobs", [])
        jobs.append(w)  # держим ссылку, пока поток работает
        w.finished.connect(lambda: jobs.remove(w) if w in jobs else None)
        w.start()

    def _report(self, res):
        if isinstance(res, Exception):
            QMessageBox.warning(self, "Ошибка", str(res))
            return
        ok, log = res
        (QMessageBox.information if ok else QMessageBox.warning)(self, "Готово" if ok else "Есть ошибки", log or "Готово.")

    def closeEvent(self, e):  # закрытие окна прячет его в трей
        e.ignore()
        self.hide()


# ------------------------------------------------------------------ tray

class TrayApp(QObject):
    def __init__(self, qapp: QApplication):
        super().__init__()
        self.qapp = qapp
        self.cfg = Config.load()
        self.prev_problems: set[tuple[str, str]] | None = None
        self.worker: Worker | None = None

        self.tray = QSystemTrayIcon(status_icon(Status.INFO))
        self.tray.setToolTip("Здоровье ноутбука: проверяю…")
        menu = QMenu()
        a_open = QAction("Открыть", menu, triggered=self.show_window)
        a_check = QAction("Проверить сейчас", menu, triggered=self.refresh)
        a_quit = QAction("Выход", menu, triggered=qapp.quit)
        a_tests = QAction("Остановить тесты…", menu, triggered=self.stop_tests_menu)
        a_swap = QAction("Вернуть swap в память…", menu, triggered=self.swap_menu)
        menu.addActions([a_open, a_check])
        menu.addSeparator()
        menu.addActions([a_tests, a_swap])
        menu.addSeparator()
        menu.addAction(a_quit)
        self.menu = menu  # держим ссылку, иначе меню соберёт сборщик мусора
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._activated)
        self.tray.messageClicked.connect(self._message_clicked)
        self.last_report = None
        self.tray.show()

        self.window = MainWindow(self)
        try:
            history.prune(self.cfg.history_days)
        except OSError:
            pass
        self.auto_clean_job: Worker | None = None
        self.timer = QTimer(self, interval=max(1, self.cfg.check_interval_min) * 60_000, timeout=self.refresh)
        self.timer.start()
        QTimer.singleShot(1500, self.refresh)

        # Быстрый сторож памяти: только /proc, раз в несколько секунд. При нехватке — сразу полная
        # проверка (она пришлёт уведомление), не чаще раза в минуту; при критической — опционально
        # автоматическая остановка тестов.
        self.last_mem_alert = 0.0
        self.auto_stop_job: Worker | None = None
        self.mem_timer = QTimer(self, interval=max(5, self.cfg.mem_watch_interval_s) * 1000,
                                timeout=self._watch_memory)
        self.mem_timer.start()

    # ---- пункты меню трея: доступны всегда

    def _bg(self, fn, cb):
        w = Worker(fn, self)
        w.done.connect(cb)
        jobs = self.__dict__.setdefault("_bg_jobs", [])
        jobs.append(w)  # держим ссылку, пока поток работает
        w.finished.connect(lambda: jobs.remove(w) if w in jobs else None)
        w.start()

    def _note(self, title: str, text: str, critical: bool = False):
        icon = QSystemTrayIcon.MessageIcon.Warning if critical else QSystemTrayIcon.MessageIcon.Information
        self.tray.showMessage(title, text, icon, 10000)

    def _ask(self, title: str, text: str) -> bool:
        box = QMessageBox(QMessageBox.Icon.Question, title, text,
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        return box.exec() == QMessageBox.StandardButton.Yes

    def stop_tests_menu(self):
        self._note("Тесты", "Ищу запущенные тесты…")
        self._bg(lambda: (testguard.find_test_processes(self.cfg), testguard.find_test_containers(self.cfg),
                          testguard.mem_snapshot()), self._stop_tests_found)

    def _stop_tests_found(self, res):
        if isinstance(res, Exception):
            self._note("Тесты", f"Ошибка поиска: {res}", True)
            return
        procs, conts, snap = res
        if not procs and not conts:
            self._note("Тесты", "Тесты не запущены — останавливать нечего.")
            return
        fix = checks.tests_fix(procs, conts, snap)
        if not self._ask(fix.label, fix.confirm + "\n\nОстановить?"):
            return
        self._bg(lambda: actions.execute(fix.root_actions, fix.user_cmds, fix.py_action),
                 lambda res: self._menu_done(res, "тесты остановлены вручную"))

    def swap_menu(self):
        snap = testguard.mem_snapshot()
        used_mb = snap.swap_used // 1024 ** 2
        if snap.swap_used < 64 * 1024 ** 2:
            self._note("Swap", f"Swap почти пуст ({used_mb} МБ) — возвращать нечего.")
            return
        if snap.available < snap.swap_used + 2 * 1024 ** 3:
            self._note("Swap", f"В swap {used_mb} МБ, а свободной памяти {snap.available // 1024 ** 2} МБ — "
                       "не поместится с запасом 2 ГБ. Сначала остановите тесты или закройте тяжёлые программы.", True)
            return
        if not self._ask("Вернуть swap в память",
                         f"В swap {used_mb} МБ, свободной памяти {snap.available // 1024 ** 2} МБ.\n"
                         "Вернуть данные из swap в память, чтобы программы перестали подтормаживать?\n"
                         "Нужен пароль, может занять минуту."):
            return
        self._bg(lambda: actions.run_root([{"action": "swap-refresh"}]),
                 lambda res: self._menu_done(res, "swap возвращён в память"))

    def _menu_done(self, res, event=""):
        if isinstance(res, Exception):
            self._note("Ошибка", str(res), True)
        else:
            ok, log = res
            if ok and event:
                log_event("action", event)
            self._note("Готово" if ok else "Есть ошибки", log or "Готово.", not ok)
        self.refresh()

    def _activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            if self.window.isVisible():
                self.window.hide()
            else:
                self.show_window()

    def show_window(self):
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def _watch_memory(self):
        snap = testguard.mem_snapshot()
        if not testguard.memory_low(snap, self.cfg):
            return
        now = time.monotonic()
        if now - self.last_mem_alert < 60:
            return
        self.last_mem_alert = now
        if (self.cfg.auto_stop_tests_on_memory_critical and testguard.memory_critical(snap, self.cfg)
                and not (self.auto_stop_job and self.auto_stop_job.isRunning())):
            self.auto_stop_job = Worker(lambda: self._auto_stop(snap), self)
            self.auto_stop_job.done.connect(self._auto_stopped)
            self.auto_stop_job.start()
        else:
            self.refresh()

    def _auto_stop(self, snap):
        procs = testguard.find_test_processes(self.cfg)
        conts = testguard.find_test_containers(self.cfg)
        if not procs and not conts:
            return None
        what = testguard.describe(procs, conts)
        log = testguard.stop_tests(procs, conts)
        return what, log

    def _auto_stopped(self, res):
        if res and not isinstance(res, Exception):
            what, _ = res
            log_event("tests", "тесты остановлены автоматически: кончалась память")
            self.tray.showMessage("Память кончалась — тесты остановлены", what[:400],
                                  QSystemTrayIcon.MessageIcon.Critical, 30000)
        self.refresh()

    def _auto_cleaned(self, res):
        if isinstance(res, Exception):
            self.tray.showMessage("Плановая очистка", f"Ошибка: {res}", QSystemTrayIcon.MessageIcon.Warning, 15000)
            return
        freed, log = res
        if freed:
            log_event("clean", f"плановая очистка: {human(freed)}")
            self.tray.showMessage(f"Плановая очистка: освобождено {human(freed)}", "\n".join(log)[:400],
                                  QSystemTrayIcon.MessageIcon.Information, 15000)

    def _message_clicked(self):
        # последнее уведомление было о сбросе — открываем разбор, иначе окно
        if self.last_report is not None:
            subprocess.Popen(["xdg-open", str(self.last_report)], start_new_session=True)
            self.last_report = None
        else:
            self.show_window()

    def _notify_reports(self, reports):
        fresh = [(b, p) for b, p in reports
                 if time.time() - b.last.timestamp() < self.cfg.reset_report_notify_hours * 3600]
        if fresh:
            b, path = fresh[-1]
            self.last_report = path
            self.tray.showMessage(f"Был жёсткий сброс {b.last:%d.%m %H:%M}",
                                  "Собран разбор: что происходило перед сбросом. Нажмите, чтобы открыть.",
                                  QSystemTrayIcon.MessageIcon.Warning, 30000)

    def refresh(self):
        if self.worker and self.worker.isRunning():
            return
        self.window.refresh_btn.setEnabled(False)
        self.worker = Worker(self._check_cycle, self)
        self.worker.done.connect(self._on_results)
        self.worker.start()

    def _check_cycle(self):
        """Фоновый поток: проверки, замер в историю, разбор новых сбросов, автоперезапуск зарядки."""
        results = checks.run_all(self.cfg)
        try:
            history.record(testguard.mem_snapshot(), top=postmortem.top_processes())
            history.record_battery_daily(self.cfg.battery)
        except OSError:
            pass
        reports = []
        boots = checks.finished_boots()
        if postmortem.new_resets(boots):
            rows = history.load(time.time() - 40 * 86400)
            drives = checks.nvme_drives()
            reports = postmortem.write_reports(boots, rows, history.load_events(0, boots),
                                               checks.nvme_smart(drives[0]) if drives else None)
        kicked = self._maybe_kick_charging(results)
        return results, reports, kicked

    def _maybe_kick_charging(self, results):
        """Батарея «Не заряжается» и включён автоперезапуск — перезапустить, не чаще раза в 30 минут."""
        if not self.cfg.auto_kick_charging:
            return None
        bat = next((r for r in results if r.id == "battery"), None)
        if not bat or bat.status != Status.ERROR or not bat.summary.startswith("Не заряжается"):
            return None
        if time.time() - state.load().get("last_charging_kick", 0) < 1800:
            return None
        state.update(last_charging_kick=time.time())
        ok, out = actions.kick_charging_nopass(self.cfg.battery)
        return out if ok else None

    def apply_config(self):
        """После сохранения настроек: интервалы таймеров и немедленная проверка."""
        self.timer.setInterval(max(1, self.cfg.check_interval_min) * 60_000)
        self.mem_timer.setInterval(max(5, self.cfg.mem_watch_interval_s) * 1000)
        self.refresh()

    def update_tray(self, results):
        """Цвет значка и подсказка — без отложенных проблем. Возвращает неотложенные результаты."""
        active = [r for r in results if not r.snoozed]
        worst = max((r.status for r in active), default=Status.OK)
        worst = Status.OK if worst == Status.INFO else worst
        self.tray.setIcon(status_icon(worst))
        self.window.setWindowIcon(status_icon(worst))
        problems = [r for r in active if r.status >= Status.WARN]
        hidden = sum(1 for r in results if r.snoozed)
        self.tray.setToolTip("Здоровье ноутбука: " + (
            "\n".join(f"• {r.title}: {r.summary}" for r in problems) if problems else "всё в порядке")
            + (f"\n(отложено: {hidden})" if hidden else ""))
        return active

    def apply_snoozes(self, results):
        sn = snooze.all_snoozes()
        for r in results:
            r.snoozed = r.status >= Status.WARN and snooze.is_snoozed(r.id, r.summary, sn)
        return results

    def _on_results(self, res):
        self.window.refresh_btn.setEnabled(True)
        if isinstance(res, Exception):
            self.tray.setToolTip(f"Ошибка проверки: {res}")
            return
        results, reports, kicked = res
        self.last_results = self.apply_snoozes(results)
        active = self.update_tray(results)
        self.window.set_results(results)
        self._notify_reports(reports)
        if kicked:
            log_event("charging", "зарядка перезапущена автоматически")
            self.tray.showMessage("Зарядка перезапущена", kicked, QSystemTrayIcon.MessageIcon.Information, 15000)
        if cleanup.auto_clean_due(self.cfg) and not (self.auto_clean_job and self.auto_clean_job.isRunning()):
            self.auto_clean_job = Worker(lambda: cleanup.run_auto_clean(self.cfg), self)
            self.auto_clean_job.done.connect(self._auto_cleaned)
            self.auto_clean_job.start()
        problems = [r for r in active if r.status >= Status.WARN]
        current = {(r.id, r.summary) for r in problems}
        new = [r for r in problems if (r.id, r.summary) not in (self.prev_problems or set())]
        if new:
            icon = QSystemTrayIcon.MessageIcon.Critical if any(r.status == Status.ERROR for r in new) \
                else QSystemTrayIcon.MessageIcon.Warning
            self.tray.showMessage("Здоровье ноутбука", "\n".join(f"{r.title}: {r.summary}" for r in new), icon, 15000)
        self.prev_problems = current


def main() -> int:
    qapp = QApplication(sys.argv)
    qapp.setApplicationName(APP_ID)
    qapp.setDesktopFileName(APP_ID)
    qapp.setQuitOnLastWindowClosed(False)

    # один экземпляр: второй запуск просто показывает окно первого
    sock = QLocalSocket()
    sock.connectToServer(APP_ID)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.waitForBytesWritten(300)
        return 0
    QLocalServer.removeServer(APP_ID)
    server = QLocalServer()
    server.listen(APP_ID)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("Системный трей недоступен", file=sys.stderr)
    app = TrayApp(qapp)

    def on_conn():
        c = server.nextPendingConnection()
        c.readyRead.connect(lambda: (c.readAll(), app.show_window()))

    server.newConnection.connect(on_conn)
    if "--show" in sys.argv:
        QTimer.singleShot(0, app.show_window)
    code = qapp.exec()
    # Фоновые проверки/подсчёты могут ещё идти (du, docker) — сохранять нечего, выходим сразу,
    # иначе Qt аварийно завершится на уничтожении работающего QThread.
    sys.stdout.flush()
    os._exit(code)
