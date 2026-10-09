"""Widgets Qt para pintar diagramas Mermaid (flowchart) con diagram.py."""

import math
import re

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen,
    QPolygonF,
)
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

import diagram

VIEW_MARGIN = 4
FALLBACK_WIDTH = 440

ROLE_COLORS = {
    "cache": ("#4a2f1b", "#c27c3a"),
    "queue": ("#3b2a4f", "#9b72cf"),
    "storage": ("#1f3d3d", "#3fa7a7"),
    "db": ("#1e3557", "#4a7bbf"),
    "client": ("#22402d", "#4f9d69"),
    "infra": ("#3d3a1f", "#b5a642"),
    "default": ("#2a2d33", "#5f6368"),
}
_ROLE_WORDS = (
    ("cache", ("redis", "memcache", "cache", "caché")),
    ("queue", ("kafka", "sqs", "queue", "cola", "stream", "pub/sub",
               "pubsub", "rabbit", "kinesis", "event bus")),
    ("storage", ("s3", "blob", "object storage", "bucket", "hdfs", "gcs")),
    ("db", ("db", "database", "base de datos", "postgres", "mysql",
            "dynamo", "cassandra", "mongo", "sql", "clickhouse", "elastic",
            "warehouse")),
    ("client", ("user", "usuario", "client", "cliente", "browser",
                "navegador", "mobile", "móvil")),
    ("infra", ("cdn", "load balancer", "balanceador", "lb", "gateway", "dns",
               "proxy", "nginx", "edge")),
)
_BOUNDARY_WORDS = {"db", "lb", "s3", "sql", "dns", "edge", "app"}


def _word_regex(word):
    escaped = re.escape(word)
    if word in _BOUNDARY_WORDS:
        return re.compile(rf"(?<!\w){escaped}(?!\w)")
    return re.compile(escaped)


_ROLE_PATTERNS = [
    (role, [_word_regex(w) for w in words]) for role, words in _ROLE_WORDS]


def node_role(label, node_id, shape):
    text = f"{label} {node_id}".lower()
    for role, patterns in _ROLE_PATTERNS:
        if any(p.search(text) for p in patterns):
            return role
    if shape == "hexagon":
        return "queue"
    if shape == "db":
        return "db"
    if shape == "stadium":
        return "client"
    return "default"


def _font(px):
    font = QFont("Segoe UI")
    font.setPixelSize(px)
    return font


def make_measure(font=None):
    metrics = QFontMetricsF(font or _font(11))
    flags = int(Qt.TextWordWrap | Qt.AlignCenter)

    def measure(label, max_w):
        rect = metrics.boundingRect(QRectF(0, 0, max_w, 10000), flags, label)
        return rect.width(), rect.height()

    return measure


def _arrow_polygon(tip, direction, size=7.0):
    dx, dy = direction
    length = math.hypot(dx, dy) or 1.0
    dx, dy = dx / length, dy / length
    bx, by = tip[0] - dx * size, tip[1] - dy * size
    half = size * 0.5
    return QPolygonF([
        QPointF(*tip),
        QPointF(bx - dy * half, by + dx * half),
        QPointF(bx + dy * half, by - dx * half),
    ])


def _shape_path(box):
    x, y, w, h = box.x, box.y, box.w, box.h
    cx, cy = x + w / 2, y + h / 2
    path = QPainterPath()
    shape = box.shape
    if shape == "stadium":
        path.addRoundedRect(QRectF(x, y, w, h), h / 2, h / 2)
    elif shape == "round":
        path.addRoundedRect(QRectF(x, y, w, h), 12, 12)
    elif shape == "circle":
        path.addEllipse(QRectF(x, y, w, h))
    elif shape == "diamond":
        path.addPolygon(QPolygonF([
            QPointF(cx, y), QPointF(x + w, cy), QPointF(cx, y + h),
            QPointF(x, cy)]))
        path.closeSubpath()
    elif shape == "hexagon":
        k = min(16.0, w * 0.2)
        path.addPolygon(QPolygonF([
            QPointF(x + k, y), QPointF(x + w - k, y), QPointF(x + w, cy),
            QPointF(x + w - k, y + h), QPointF(x + k, y + h),
            QPointF(x, cy)]))
        path.closeSubpath()
    elif shape == "para":
        k = min(12.0, w * 0.15)
        path.addPolygon(QPolygonF([
            QPointF(x + k, y), QPointF(x + w, y), QPointF(x + w - k, y + h),
            QPointF(x, y + h)]))
        path.closeSubpath()
    elif shape == "asym":
        k = min(12.0, w * 0.15)
        path.addPolygon(QPolygonF([
            QPointF(x, y), QPointF(x + w, y), QPointF(x + w, y + h),
            QPointF(x, y + h), QPointF(x + k, cy)]))
        path.closeSubpath()
    elif shape == "db":
        e = min(12.0, h / 3)
        path.moveTo(x, y + e / 2)
        path.arcTo(QRectF(x, y, w, e), 180, -180)
        path.lineTo(x + w, y + h - e / 2)
        path.arcTo(QRectF(x, y + h - e, w, e), 0, -180)
        path.closeSubpath()
    else:
        path.addRoundedRect(QRectF(x, y, w, h), 6, 6)
    return path


class DiagramView(QWidget):
    height_changed = Signal()

    def __init__(self, graph, parent=None):
        super().__init__(parent)
        self.graph = graph
        self._node_font = _font(11)
        self._small_font = _font(10)
        measure = make_measure(self._node_font)
        self._layouts = {
            "TD": diagram.layout_graph(graph, measure, "TD"),
            "LR": diagram.layout_graph(graph, measure, "LR"),
        }
        self._direction = "LR" if graph.direction == "LR" else "TD"
        self._scale = 1.0
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._relayout(FALLBACK_WIDTH)

    @property
    def layout_in_use(self):
        return self._layouts[self._direction]

    @property
    def direction(self):
        return self._direction

    @property
    def scale(self):
        return self._scale

    def natural_width(self):
        return self._layouts["TD"].width + 2 * VIEW_MARGIN

    def _choose(self, available):
        scales = {
            name: min(1.0, available / lay.width)
            for name, lay in self._layouts.items()}
        declared = "LR" if self.graph.direction == "LR" else "TD"
        if scales["TD"] >= 1.0 and scales["LR"] >= 1.0:
            return declared, 1.0
        if scales["LR"] > 1.15 * scales["TD"]:
            return "LR", scales["LR"]
        return "TD", scales["TD"]

    def _relayout(self, width):
        available = max(40.0, width - 2 * VIEW_MARGIN)
        self._direction, self._scale = self._choose(available)
        height = round(
            self.layout_in_use.height * self._scale + 2 * VIEW_MARGIN)
        if self.height() != height:
            self.setFixedHeight(height)
            self.height_changed.emit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout(event.size().width())
        self.update()

    def sizeHint(self):
        return QSize(int(self.natural_width()),
                     round(self.layout_in_use.height * self._scale
                           + 2 * VIEW_MARGIN))

    def minimumSizeHint(self):
        return QSize(120, self.height())

    # ------------------------------ pintado ------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        self.paint_diagram(painter, self.width(), self.height())
        painter.end()

    def paint_diagram(self, painter, width, height):
        lay = self.layout_in_use
        painter.setPen(QPen(QColor("#2f3237"), 1))
        painter.setBrush(QColor("#1f2124"))
        painter.drawRoundedRect(QRectF(0.5, 0.5, width - 1, height - 1), 8, 8)

        painter.save()
        scale = self._scale
        offset_x = max(VIEW_MARGIN, (width - lay.width * scale) / 2)
        painter.translate(offset_x, VIEW_MARGIN)
        painter.scale(scale, scale)
        self._paint_groups(painter, lay)
        self._paint_edges(painter, lay)
        self._paint_nodes(painter, lay)
        painter.restore()

    def _paint_groups(self, painter, lay):
        for group in lay.groups:
            rect = QRectF(group.x, group.y, group.w, group.h)
            painter.setPen(QPen(QColor("#3a3d42"), 1, Qt.DashLine))
            painter.setBrush(QColor("#23262b"))
            painter.drawRoundedRect(rect, 8, 8)
            painter.setFont(self._small_font)
            painter.setPen(QColor("#9aa0a6"))
            painter.drawText(
                QRectF(group.x + 8, group.y + 2, group.w - 12, 16),
                int(Qt.AlignLeft | Qt.AlignVCenter), group.title)

    def _edge_path(self, edge, td):
        pts = edge.points
        path = QPainterPath(QPointF(*pts[0]))
        for p, q in zip(pts, pts[1:]):
            if td:
                dy = (q[1] - p[1]) / 2
                path.cubicTo(QPointF(p[0], p[1] + dy), QPointF(q[0], q[1] - dy),
                             QPointF(*q))
            else:
                dx = (q[0] - p[0]) / 2
                path.cubicTo(QPointF(p[0] + dx, p[1]), QPointF(q[0] - dx, q[1]),
                             QPointF(*q))
        return path

    @staticmethod
    def _tangent(p, q, td):
        if td:
            return (0, 1 if q[1] >= p[1] else -1)
        return (1 if q[0] >= p[0] else -1, 0)

    def _paint_edges(self, painter, lay):
        td = lay.direction == "TD"
        color = QColor("#7c8087")
        for edge in lay.edges:
            if len(edge.points) < 2:
                continue
            if edge.style == "thick":
                pen = QPen(color, 2.4)
            elif edge.style == "dotted":
                pen = QPen(color, 1.2, Qt.DashLine)
            else:
                pen = QPen(color, 1.4)
            pen.setCapStyle(Qt.RoundCap)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(self._edge_path(edge, td))
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            first, last = edge.points[0], edge.points[-1]
            end_dir = self._tangent(edge.points[-2], last, td)
            start_dir = self._tangent(edge.points[1], first, td)
            if edge.head:
                painter.drawPolygon(_arrow_polygon(last, end_dir))
            if edge.tail:
                painter.drawPolygon(_arrow_polygon(first, start_dir))
        painter.setFont(self._small_font)
        for edge in lay.edges:
            if not edge.label or edge.label_pos is None:
                continue
            w, h = edge.label_size
            rect = QRectF(edge.label_pos[0] - w / 2, edge.label_pos[1] - h / 2,
                          w, h)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#181a1d"))
            painter.drawRoundedRect(rect, 4, 4)
            painter.setPen(QColor("#bdc1c6"))
            painter.drawText(rect, int(Qt.AlignCenter | Qt.TextWordWrap),
                             edge.label)

    def _paint_nodes(self, painter, lay):
        painter.setFont(self._node_font)
        for box in lay.nodes.values():
            fill, border = ROLE_COLORS[node_role(box.label, box.id, box.shape)]
            painter.setPen(QPen(QColor(border), 1.2))
            painter.setBrush(QBrush(QColor(fill)))
            path = _shape_path(box)
            painter.drawPath(path)
            if box.shape == "db":
                e = min(12.0, box.h / 3)
                rim = QPainterPath()
                rim.moveTo(box.x, box.y + e / 2)
                rim.arcTo(QRectF(box.x, box.y, box.w, e), 180, 180)
                painter.setBrush(Qt.NoBrush)
                painter.drawPath(rim)
            elif box.shape == "subroutine":
                painter.drawLine(QPointF(box.x + 6, box.y),
                                 QPointF(box.x + 6, box.y + box.h))
                painter.drawLine(QPointF(box.x + box.w - 6, box.y),
                                 QPointF(box.x + box.w - 6, box.y + box.h))
            text_rect = QRectF(box.x + 6, box.y + 2, box.w - 12, box.h - 4)
            if box.shape == "db":
                text_rect.adjust(0, 6, 0, 0)
            painter.setPen(QColor("#e8eaed"))
            painter.drawText(text_rect, int(Qt.AlignCenter | Qt.TextWordWrap),
                             box.label)


class DiagramStack(QWidget):
    height_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sources = []
        self.views = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(6)
        self._layout = layout
        self.caption = QLabel("", self)
        self.caption.setStyleSheet(
            "color: #8e9297; font-size: 11px; background: transparent;")
        self.caption.hide()
        layout.addWidget(self.caption)
        self.placeholder = QLabel("✎ dibujando diagrama…", self)
        self.placeholder.setStyleSheet(
            "color: #9aa0a6; font-size: 11px; background: transparent;")
        self.placeholder.hide()
        layout.addWidget(self.placeholder)
        self.hide()

    def set_caption(self, text):
        self.caption.setText(text or "")

    def set_sources(self, graphs_by_source, drawing):
        sources = list(graphs_by_source)
        if sources != self._sources:
            for view in self.views:
                self._layout.removeWidget(view)
                view.setParent(None)
                view.deleteLater()
            self.views = []
            for source in sources:
                view = DiagramView(graphs_by_source[source], self)
                view.height_changed.connect(self.height_changed)
                self._layout.insertWidget(self._layout.count() - 1, view)
                self.views.append(view)
            self._sources = sources
        self.placeholder.setVisible(bool(drawing))
        visible = bool(self.views) or bool(drawing)
        self.caption.setVisible(visible and bool(self.caption.text()))
        if visible == self.isHidden():
            self.setVisible(visible)
        self.height_changed.emit()

    def natural_width(self):
        return max((v.natural_width() for v in self.views), default=0.0)
