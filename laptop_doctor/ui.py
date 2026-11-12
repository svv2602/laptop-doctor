"""KDE tray app: status icon + window with checks and cleanup."""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import os
import subprocess
import time

from PyQt6.QtCore import QObject, QPointF, QRectF, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QFontDatabase, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QHBoxLayout, QHeaderView, QLabel, QMainWindow,
    QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QSystemTrayIcon, QTableWidget,
    QTableWidgetItem, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import actions, checks, cleanup, testguard
from .checks import STATUS_LABEL, CheckResult, Status
from .config import CONFIG_PATH, Config
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
        cfg_btn.setToolTip(str(CONFIG_PATH))
        cfg_btn.clicked.connect(lambda: subprocess.Popen(["xdg-open", str(CONFIG_PATH)]))
        top.addWidget(self.refresh_btn)
        top.addWidget(self.updated_lbl, 1)
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
        for r in sorted(results, key=lambda r: -r.status):
            it = QTreeWidgetItem([STATUS_LABEL[r.status], r.title, r.summary])
            it.setIcon(0, status_icon(r.status, 32))
            it.setData(0, Qt.ItemDataRole.UserRole, r.id)
            if r.status >= Status.WARN:
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
        self.details.setPlainText(f"{r.title}: {r.summary}\n\n{r.details}".rstrip())
        self.fix_btn.setVisible(r.fix is not None)
        if r.fix:
            self.fix_btn.setText(r.fix.label + ("  🔒" if r.fix.root_actions else ""))

    def _run_fix(self):
        r = self._current()
        if not r or not r.fix:
            return
        if QMessageBox.question(self, r.fix.label, r.fix.confirm + "\n\nВыполнить?") != QMessageBox.StandardButton.Yes:
            return
        fix = r.fix
        self.fix_btn.setEnabled(False)
        self._job(lambda: actions.execute(fix.root_actions, fix.user_cmds, fix.py_action), self._fix_done)

    def _fix_done(self, res):
        self.fix_btn.setEnabled(True)
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
        self.estimate()
        self.app.refresh()

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
        self.tray.messageClicked.connect(self.show_window)
        self.tray.show()

        self.window = MainWindow(self)
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
        self._bg(lambda: actions.execute(fix.root_actions, fix.user_cmds, fix.py_action), self._menu_done)

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
        self._bg(lambda: actions.run_root([{"action": "swap-refresh"}]), self._menu_done)

    def _menu_done(self, res):
        if isinstance(res, Exception):
            self._note("Ошибка", str(res), True)
        else:
            ok, log = res
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
            self.tray.showMessage("Память кончалась — тесты остановлены", what[:400],
                                  QSystemTrayIcon.MessageIcon.Critical, 30000)
        self.refresh()

    def refresh(self):
        if self.worker and self.worker.isRunning():
            return
        self.window.refresh_btn.setEnabled(False)
        self.worker = Worker(lambda: checks.run_all(self.cfg), self)
        self.worker.done.connect(self._on_results)
        self.worker.start()

    def _on_results(self, results):
        self.window.refresh_btn.setEnabled(True)
        if isinstance(results, Exception):
            self.tray.setToolTip(f"Ошибка проверки: {results}")
            return
        worst = max((r.status for r in results), default=Status.OK)
        worst = Status.OK if worst == Status.INFO else worst
        self.tray.setIcon(status_icon(worst))
        self.window.setWindowIcon(status_icon(worst))
        self.window.set_results(results)
        problems = [r for r in results if r.status >= Status.WARN]
        self.tray.setToolTip("Здоровье ноутбука: " + (
            "\n".join(f"• {r.title}: {r.summary}" for r in problems) if problems else "всё в порядке"))
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
