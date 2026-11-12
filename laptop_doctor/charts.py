"""Small hand-drawn Qt charts: one measure per chart, one axis, hover tooltip.

Colors follow the validated reference palette (dataviz skill): categorical slot 1 blue,
recessive grid and axes, text in ink tokens — never in the series color.
"""
from __future__ import annotations

import math
from datetime import date, datetime

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QApplication, QSizePolicy, QToolTip, QWidget

LIGHT = {"surface": "#fcfcfb", "series": "#2a78d6", "ink": "#0b0b0b", "ink2": "#52514e",
         "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7"}
DARK = {"surface": "#1a1a19", "series": "#3987e5", "ink": "#f0efec", "ink2": "#c3c2b7",
        "muted": "#898781", "grid": "#2c2c2a", "axis": "#383835"}


def theme() -> dict:
    bg = QApplication.palette().window().color()
    return DARK if bg.lightness() < 128 else LIGHT


def nice_ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    """Круглые деления оси: шаг 1/2/2.5/5 × 10^k, покрывающие [lo, hi]."""
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw)
    first = math.floor(lo / step) * step
    count = math.ceil((hi - first) / step - 1e-9)
    return [round(first + i * step, 6) for i in range(count + 1)]


class _Base(QWidget):
    PAD_L, PAD_R, PAD_T, PAD_B = 48, 14, 30, 26

    def __init__(self, title: str, unit: str, parent=None):
        super().__init__(parent)
        self.title, self.unit = title, unit
        self.empty_text = "Нет данных"
        self.setMinimumHeight(170)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.hover: int | None = None

    def plot_rect(self) -> QRectF:
        return QRectF(self.PAD_L, self.PAD_T, self.width() - self.PAD_L - self.PAD_R,
                      self.height() - self.PAD_T - self.PAD_B)

    def _frame(self, p: QPainter, t: dict) -> None:
        p.fillRect(self.rect(), QColor(t["surface"]))
        f = QFont(self.font())
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(t["ink"]))
        p.drawText(QRectF(8, 4, self.width() - 16, 22), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self.title)
        p.setFont(self.font())

    def _y_axis(self, p: QPainter, t: dict, ticks: list[float], y_of) -> None:
        r = self.plot_rect()
        for v in ticks:
            y = y_of(v)
            p.setPen(QPen(QColor(t["grid"]), 1))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(QColor(t["muted"]))
            label = f"{v:g}{self.unit}"
            p.drawText(QRectF(0, y - 8, self.PAD_L - 6, 16),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, label)
        p.setPen(QPen(QColor(t["axis"]), 1))
        p.drawLine(QPointF(r.left(), r.bottom()), QPointF(r.right(), r.bottom()))

    def _empty(self, p: QPainter, t: dict) -> None:
        p.setPen(QColor(t["muted"]))
        p.drawText(self.plot_rect(), Qt.AlignmentFlag.AlignCenter, self.empty_text)

    def leaveEvent(self, e):
        self.hover = None
        QToolTip.hideText()
        self.update()


class LineChart(_Base):
    """Временной ряд. Разрыв линии, если между точками больше gap_s (ноут был выключен)."""

    def __init__(self, title: str, unit: str, fmt: str = "{:.0f}", y_min: float | None = 0,
                 y_max: float | None = None, parent=None):
        super().__init__(title, unit, parent)
        self.fmt, self.y_min, self.y_max = fmt, y_min, y_max
        self.points: list[tuple[float, float]] = []
        self.t0 = self.t1 = 0.0
        self.gap_s = 1800.0

    def set_data(self, points, t0: float, t1: float, gap_s: float):
        self.points, self.t0, self.t1, self.gap_s = sorted(points), t0, t1, gap_s
        self.hover = None
        self.update()

    def _scales(self):
        r = self.plot_rect()
        vals = [v for _, v in self.points]
        lo = self.y_min if self.y_min is not None else min(vals)
        hi = self.y_max if self.y_max is not None else max(vals) * 1.1 or 1
        ticks = nice_ticks(lo, hi)
        lo, hi = min(lo, ticks[0]), max(hi, ticks[-1])
        span_t = (self.t1 - self.t0) or 1
        x_of = lambda ts: r.left() + (ts - self.t0) / span_t * r.width()  # noqa: E731
        y_of = lambda v: r.bottom() - (v - lo) / ((hi - lo) or 1) * r.height()  # noqa: E731
        return ticks, x_of, y_of

    def paintEvent(self, _):
        t = theme()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._frame(p, t)
        if not self.points:
            self._empty(p, t)
            return
        ticks, x_of, y_of = self._scales()
        self._y_axis(p, t, ticks, y_of)
        self._x_axis(p, t, x_of)
        path, prev = QPainterPath(), None
        for ts, v in self.points:
            pt = QPointF(x_of(ts), y_of(v))
            if prev is None or ts - prev > self.gap_s:
                path.moveTo(pt)
            else:
                path.lineTo(pt)
            prev = ts
        pen = QPen(QColor(t["series"]), 2)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.drawPath(path)
        if self.hover is not None:
            ts, v = self.points[self.hover]
            x, y = x_of(ts), y_of(v)
            r = self.plot_rect()
            p.setPen(QPen(QColor(t["axis"]), 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.setPen(QPen(QColor(t["surface"]), 2))
            p.setBrush(QColor(t["series"]))
            p.drawEllipse(QPointF(x, y), 5, 5)

    def _x_axis(self, p: QPainter, t: dict, x_of) -> None:
        r = self.plot_rect()
        span = self.t1 - self.t0
        p.setPen(QColor(t["muted"]))
        if span <= 2 * 86400:
            step, fmt = 3 * 3600 if span > 86400 / 2 else 3600, "%H:%M"
        else:
            step, fmt = 86400 if span <= 10 * 86400 else 5 * 86400, "%d.%m"
        start = datetime.fromtimestamp(self.t0)
        first = (start.replace(minute=0, second=0, microsecond=0).timestamp() if step < 86400
                 else start.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        ts, last_x = first, -1e9
        while ts <= self.t1:
            if ts >= self.t0:
                x = x_of(ts)
                if x - last_x > 46:
                    p.drawText(QRectF(x - 30, r.bottom() + 4, 60, 18), Qt.AlignmentFlag.AlignHCenter,
                               datetime.fromtimestamp(ts).strftime(fmt))
                    last_x = x
            ts += step

    def mouseMoveEvent(self, e):
        if not self.points:
            return
        _, x_of, _ = self._scales()
        mx = e.position().x()
        i = min(range(len(self.points)), key=lambda k: abs(x_of(self.points[k][0]) - mx))
        if abs(x_of(self.points[i][0]) - mx) > 40:
            self.leaveEvent(e)
            return
        self.hover = i
        ts, v = self.points[i]
        QToolTip.showText(e.globalPosition().toPoint(),
                          f"{datetime.fromtimestamp(ts):%d.%m %H:%M}\n{self.title}: {self.fmt.format(v)}{self.unit}", self)
        self.update()


class DayBarChart(_Base):
    """Столбики по дням (счётчик)."""

    def __init__(self, title: str, parent=None):
        super().__init__(title, "", parent)
        self.days: list[tuple[date, int]] = []

    def set_data(self, days: list[tuple[date, int]]):
        self.days = days
        self.update()

    def _geom(self):
        r = self.plot_rect()
        n = max(len(self.days), 1)
        slot = r.width() / n
        bw = max(min(slot - 4, 28), 3)  # промежуток между столбиками ≥ 2px
        return r, slot, bw

    def paintEvent(self, _):
        t = theme()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._frame(p, t)
        if not self.days:
            self._empty(p, t)
            return
        hi = max(max(c for _, c in self.days), 2)
        ticks = [v for v in nice_ticks(0, hi, 2) if float(v).is_integer()]
        r, slot, bw = self._geom()
        y_of = lambda v: r.bottom() - v / (ticks[-1] or 1) * r.height()  # noqa: E731
        self._y_axis(p, t, ticks, y_of)
        p.setPen(QColor(t["muted"]))
        every = max(1, int(46 // slot) + 1)
        for i, (d, c) in enumerate(self.days):
            cx = r.left() + slot * (i + 0.5)
            if c:
                top = y_of(c)
                color = QColor(t["series"])
                if self.hover == i:
                    color = color.lighter(115)
                rect = QRectF(cx - bw / 2, top, bw, r.bottom() - top)
                # скругление 4px только сверху: скруглённый прямоугольник + прямоугольник ниже радиуса
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(color)
                p.drawRoundedRect(QRectF(rect.left(), rect.top(), rect.width(), min(rect.height(), 8)), 4, 4)
                if rect.height() > 4:
                    p.drawRect(QRectF(rect.left(), rect.top() + 4, rect.width(), rect.height() - 4))
                p.setBrush(Qt.BrushStyle.NoBrush)
            if i % every == 0 or i == len(self.days) - 1:
                p.setPen(QColor(t["muted"]))
                p.drawText(QRectF(cx - 30, r.bottom() + 4, 60, 18), Qt.AlignmentFlag.AlignHCenter, d.strftime("%d.%m"))

    def mouseMoveEvent(self, e):
        if not self.days:
            return
        r, slot, _ = self._geom()
        i = int((e.position().x() - r.left()) // slot)
        if not 0 <= i < len(self.days):
            self.leaveEvent(e)
            return
        self.hover = i
        d, c = self.days[i]
        QToolTip.showText(e.globalPosition().toPoint(), f"{d:%d.%m.%Y}\n{self.title}: {c}", self)
        self.update()
