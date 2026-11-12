"""Small hand-drawn Qt charts: one measure per chart, one axis, hover tooltip.

Colors follow the validated reference palette (dataviz skill): categorical slot 1 blue,
recessive grid and axes, text in ink tokens — never in the series color.
"""
from __future__ import annotations

import math
from datetime import date, datetime

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PyQt6.QtWidgets import QApplication, QSizePolicy, QStackedWidget, QToolTip, QWidget

LIGHT = {"surface": "#fcfcfb", "series": "#2a78d6", "series2": "#eb6834", "neutral": "#f0efec",
         "track": "#cde2fb", "ink": "#0b0b0b", "ink2": "#52514e",
         "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7"}
DARK = {"surface": "#1a1a19", "series": "#3987e5", "series2": "#d95926", "neutral": "#383835",
        "track": "#104281", "ink": "#f0efec", "ink2": "#c3c2b7",
        "muted": "#898781", "grid": "#2c2c2a", "axis": "#383835"}


CRITICAL = "#d03b3b"  # статус «критично» — только для жёстких сбросов, всегда с подписью в подсказке


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
        self._tick_labels: list[str] = []

    def plot_rect(self) -> QRectF:
        fm = self.fontMetrics()
        pad_l = max(self.PAD_L, max((fm.horizontalAdvance(x) for x in self._tick_labels), default=0) + 12)
        pad_t = max(self.PAD_T, fm.height() + 12)
        pad_b = max(self.PAD_B, fm.height() + 8)
        return QRectF(pad_l, pad_t, self.width() - pad_l - self.PAD_R, self.height() - pad_t - pad_b)

    def _frame(self, p: QPainter, t: dict) -> None:
        p.fillRect(self.rect(), QColor(t["surface"]))
        f = QFont(self.font())
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(t["ink"]))
        p.drawText(QRectF(8, 4, self.width() - 16, self.fontMetrics().height() + 6),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.title)
        p.setFont(self.font())

    def tick_label(self, v: float) -> str:
        return f"{v:g}{self.unit}"

    def _y_axis(self, p: QPainter, t: dict, ticks: list[float], y_of) -> None:
        r = self.plot_rect()
        for v in ticks:
            y = y_of(v)
            p.setPen(QPen(QColor(t["grid"]), 1))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(QColor(t["muted"]))
            label = self.tick_label(v)
            p.drawText(QRectF(0, y - 8, r.left() - 6, 16),
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

    X_PAD = 10  # отступ данных от левого/правого края области графика — свежая точка не прилипает к рамке
    Y_PAD = 6

    def __init__(self, title: str, unit: str, fmt: str = "{:.0f}", y_min: float | None = 0,
                 y_max: float | None = None, min_span: float = 1.0, parent=None):
        super().__init__(title, unit, parent)
        self.fmt, self.y_min, self.y_max, self.min_span = fmt, y_min, y_max, min_span
        self.points: list[tuple[float, float]] = []
        self.events: list[tuple[float, str, str]] = []   # (время, вид, текст); вид "reset" — красная линия
        self.t0 = self.t1 = 0.0
        self.gap_s = 1800.0

    def set_data(self, points, t0: float, t1: float, gap_s: float, events=()):
        self.points, self.t0, self.t1, self.gap_s = sorted(points), t0, t1, gap_s
        self.events = [e for e in events if t0 <= e[0] <= t1]
        self.hover = None
        self.update()

    def _scales(self):
        vals = [v for _, v in self.points]
        vmin, vmax = min(vals), max(vals)
        lo = self.y_min if self.y_min is not None else vmin - (vmax - vmin) * 0.1
        if self.y_max is not None:
            hi = self.y_max
        else:
            # запас сверху 15% и минимальный размах: почти пустой swap не растягивается на всю высоту
            hi = max(vmax + (vmax - lo) * 0.15, lo + self.min_span)
        ticks = nice_ticks(lo, hi)
        lo, hi = min(lo, ticks[0]), max(hi, ticks[-1])
        self._tick_labels = [self.tick_label(v) for v in ticks]
        r = self.plot_rect()  # после подписей: отступ слева зависит от их ширины
        span_t = (self.t1 - self.t0) or 1
        left, width = r.left() + self.X_PAD, r.width() - 2 * self.X_PAD
        x_of = lambda ts: left + (ts - self.t0) / span_t * width  # noqa: E731
        # отступ по вертикали: значение на пределе шкалы (100%) не сливается с рамкой и не срезается
        top, height = r.top() + self.Y_PAD, r.height() - 2 * self.Y_PAD
        y_of = lambda v: top + height - (v - lo) / ((hi - lo) or 1) * height  # noqa: E731
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
        # отрезки между разрывами; одиночную точку рисуем кружком, иначе её не видно
        segments: list[list[QPointF]] = []
        prev = None
        for ts, v in self.points:
            pt = QPointF(x_of(ts), y_of(v))
            if prev is None or ts - prev > self.gap_s:
                segments.append([pt])
            else:
                segments[-1].append(pt)
            prev = ts
        p.save()
        p.setClipRect(self.plot_rect().adjusted(-3, -3, 3, 3))
        r = self.plot_rect()
        for ts, kind, _ in self.events:  # отметки событий — под линией данных
            x = x_of(ts)
            if kind == "reset":
                p.setPen(QPen(QColor(CRITICAL), 1.5))
            else:
                p.setPen(QPen(QColor(t["muted"]), 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
        pen = QPen(QColor(t["series"]), 2)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        for seg in segments:
            if len(seg) == 1:
                p.setBrush(QColor(t["series"]))
                p.drawEllipse(seg[0], 2.5, 2.5)
                p.setBrush(Qt.BrushStyle.NoBrush)
            else:
                p.drawPolyline(seg)
        p.restore()
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
        near = [ev for ev in self.events if abs(x_of(ev[0]) - mx) <= 4]
        if near:
            self.hover = None
            QToolTip.showText(e.globalPosition().toPoint(), "\n".join(
                f"{datetime.fromtimestamp(ts):%d.%m %H:%M} — {text}" for ts, _, text in near), self)
            self.update()
            return
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
        self._tick_labels = [self.tick_label(v) for v in ticks]
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


# ------------------------------------------------------------------ диаграмма текущего состояния

class SnapshotPanel(QWidget):
    """Когда истории за период мало: текущее состояние вместо пустого графика.

    Сверху вниз: заголовок, крупная строка-итог, высокая сегментная полоса (часть-целое, ≤ 3 частей,
    легенда из названий), шкалы-метры (одно отношение к пределу), столбики по убыванию (один цвет).
    """

    BAR_H = 28       # высокая сегментная полоса — не выше 28px
    METER_H = 10
    ROW_H = 18

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.title = title
        self.headline = ""
        self.note = ""
        self.parts: list[tuple[str, float, str, str]] = []    # (название, значение, ключ цвета, текст)
        self.meters: list[tuple[str, float, str]] = []         # (название, доля 0..1, текст)
        self.bars: list[tuple[str, float, str]] = []           # (название, значение, текст)
        self.bars_title = ""
        self._hits: list[tuple[QRectF, str]] = []
        self.setMinimumHeight(190)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)

    def set_data(self, headline="", parts=(), meters=(), bars=(), bars_title="", note=""):
        self.headline, self.note, self.bars_title = headline, note, bars_title
        self.parts, self.meters, self.bars = list(parts), list(meters), list(bars)
        self.update()

    def paintEvent(self, _):
        t = theme()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(t["surface"]))
        fm = self.fontMetrics()
        lh = fm.height()
        x0, w = 8.0, self.width() - 16.0
        y = 4.0
        bold = QFont(self.font())
        bold.setBold(True)
        p.setFont(bold)
        p.setPen(QColor(t["ink"]))
        p.drawText(QRectF(x0, y, w, lh + 6), Qt.AlignmentFlag.AlignVCenter, self.title)
        p.setFont(self.font())
        y += lh + 10
        self._hits = []
        if self.headline:
            big = QFont(self.font())
            big.setPointSizeF(self.font().pointSizeF() * 1.5)
            p.setFont(big)
            p.setPen(QColor(t["ink"]))
            bh = QFontMetricsF(big).height()
            p.drawText(QRectF(x0, y, w, bh), Qt.AlignmentFlag.AlignVCenter, self.headline)
            p.setFont(self.font())
            y += bh + 8
        if self.parts:
            y = self._segmented(p, t, x0, y, w, lh)
        for name, frac, text in self.meters:
            y = self._meter(p, t, x0, y, w, lh, name, frac, text)
        if self.bars:
            y = self._ranked(p, t, x0, y, w, lh)
        if self.note:
            p.setPen(QColor(t["muted"]))
            nr = self._note_rect(w)
            p.drawText(nr, Qt.AlignmentFlag.AlignBottom | Qt.TextFlag.TextWordWrap, self.note)

    def _note_rect(self, w: float) -> QRectF:
        """Подпись внизу с переносом строк; столбики не заходят на неё."""
        if not self.note:
            return QRectF(8, self.height(), w, 0)
        br = self.fontMetrics().boundingRect(0, 0, int(w), 1000, int(Qt.TextFlag.TextWordWrap), self.note)
        return QRectF(8, self.height() - br.height() - 6, w, br.height())

    def _segmented(self, p, t, x0, y, w, lh):
        total = sum(v for _, v, _, _ in self.parts) or 1
        x = x0
        gap = 2.0  # зазор цвета фона между частями
        visible = [part for part in self.parts if part[1] > 0]
        for i, (name, v, key, text) in enumerate(visible):
            seg_w = max(v / total * (w - gap * (len(visible) - 1)), 1)
            rect = QRectF(x, y, seg_w, self.BAR_H)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t[key]))
            p.drawRoundedRect(rect, 4, 4)
            share = f"{v / total * 100:.0f}%"
            ink = "#0b0b0b" if key == "neutral" and t is LIGHT else ("#f0efec" if key == "neutral" else "#ffffff")
            if self.fontMetrics().horizontalAdvance(share) + 12 < seg_w:
                p.setPen(QColor(ink))
                p.drawText(rect.adjusted(6, 0, -4, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, share)
            self._hits.append((rect, f"{name}: {text} ({share})"))
            x += seg_w + gap
        y += self.BAR_H + 6
        # легенда: только названия, в порядке частей
        lx = x0
        for name, v, key, _ in visible:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t[key]))
            p.drawRoundedRect(QRectF(lx, y + lh / 2 - 5, 10, 10), 2, 2)
            p.setPen(QColor(t["ink2"]))
            tw = self.fontMetrics().horizontalAdvance(name)
            p.drawText(QRectF(lx + 14, y, tw + 4, lh), Qt.AlignmentFlag.AlignVCenter, name)
            lx += 14 + tw + 18
        return y + lh + 10

    def _meter(self, p, t, x0, y, w, lh, name, frac, text):
        p.setPen(QColor(t["ink2"]))
        p.drawText(QRectF(x0, y, w, lh), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, name)
        p.setPen(QColor(t["ink"]))
        p.drawText(QRectF(x0, y, w, lh), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, text)
        y += lh + 3
        track = QRectF(x0, y, w, self.METER_H)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(t["track"]))
        p.drawRoundedRect(track, self.METER_H / 2, self.METER_H / 2)
        frac = max(0.0, min(1.0, frac))
        if frac > 0:
            p.setBrush(QColor(t["series"]))
            p.drawRoundedRect(QRectF(x0, y, max(w * frac, self.METER_H), self.METER_H), self.METER_H / 2, self.METER_H / 2)
        self._hits.append((track, f"{name}: {text}"))
        return y + self.METER_H + 10

    def _ranked(self, p, t, x0, y, w, lh):
        if self.bars_title:
            p.setPen(QColor(t["ink2"]))
            p.drawText(QRectF(x0, y, w, lh), Qt.AlignmentFlag.AlignVCenter, self.bars_title)
            y += lh + 4
        fm = self.fontMetrics()
        name_w = min(max(fm.horizontalAdvance(n) for n, _, _ in self.bars) + 10, w * 0.4)
        val_w = max(fm.horizontalAdvance(tx) for _, _, tx in self.bars) + 8
        vmax = max(v for _, v, _ in self.bars) or 1
        row = max(lh, self.ROW_H) + 4
        for name, v, text in self.bars:
            if y + row > self._note_rect(w).top() - 2:
                break
            p.setPen(QColor(t["ink2"]))
            p.drawText(QRectF(x0, y, name_w - 6, row), Qt.AlignmentFlag.AlignVCenter,
                       fm.elidedText(name, Qt.TextElideMode.ElideRight, int(name_w - 6)))
            bw = max((w - name_w - val_w) * v / vmax, 2)
            rect = QRectF(x0 + name_w, y + (row - 12) / 2, bw, 12)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t["series"]))
            p.drawRoundedRect(rect, 3, 3)
            p.setPen(QColor(t["ink"]))
            p.drawText(QRectF(rect.right() + 6, y, val_w, row), Qt.AlignmentFlag.AlignVCenter, text)
            self._hits.append((QRectF(x0, y, w, row), f"{name}: {text}"))
            y += row
        return y + 4

    def mouseMoveEvent(self, e):
        pos = e.position()
        for rect, tip in self._hits:
            if rect.contains(pos):
                QToolTip.showText(e.globalPosition().toPoint(), tip, self)
                return
        QToolTip.hideText()


class ChartSlot(QStackedWidget):
    """Ячейка сетки: график, а пока истории мало — диаграмма текущего состояния."""

    def __init__(self, chart: QWidget, snapshot: SnapshotPanel | None, parent=None):
        super().__init__(parent)
        self.chart, self.snapshot = chart, snapshot
        self.addWidget(chart)
        if snapshot is not None:
            self.addWidget(snapshot)
        self.setMinimumHeight(300 if snapshot is not None else 240)

    def show_chart(self, points, t0, t1, gap_s, events=()):
        self.chart.set_data(points, t0, t1, gap_s, events)
        self.setCurrentWidget(self.chart)

    def show_snapshot(self):
        if self.snapshot is not None:
            self.setCurrentWidget(self.snapshot)
