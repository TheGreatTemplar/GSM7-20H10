"""Persistent point annotations shared by measurement plots."""

import math

from PySide6 import QtCore, QtWidgets
import pyqtgraph as pg
from shiboken6 import isValid


class PointHover(QtCore.QObject):
    def __init__(self, plot, formatter):
        super().__init__(plot)
        self.plot, self.formatter = plot, formatter
        self.selection = None
        self.selected_scatter = None
        self.scatters = []
        self.label = QtWidgets.QLabel(plot.viewport())
        self.label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.label.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.label.setStyleSheet("""
            QLabel
            {
                background: #223247;
                color: #ffffff;
                border: 1px solid #f4bd61;
                border-radius: 4px;
                padding: 6px;
            }
        """)
        self.label.hide()
        plot.viewport().setMouseTracking(True)
        self.lines = [QtWidgets.QGraphicsLineItem(), QtWidgets.QGraphicsLineItem()]
        for line in self.lines:
            line.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
            line.setPen(pg.mkPen("#f4bd61", width=1, style=QtCore.Qt.PenStyle.DashLine))
            line.setZValue(20)
            line.hide()
            plot.addItem(line, ignoreBounds=True)
        plot.viewport().installEventFilter(self)
        plot.getViewBox().sigRangeChanged.connect(self.position)
        plot.getViewBox().sigResized.connect(self.position)

    def attach(self, curve, x, y, data):
        curve.scatter.setData(x=x, y=y, data=data, hoverable=True, tip=None,
            size=8, hoverSize=20, hoverBrush=pg.mkBrush("#f4bd61"))
        channel = data[0].get("channel", 0) if data else 0
        curve.scatter.setZValue(50 + channel)
        if curve.scatter not in self.scatters:
            self.scatters.append(curve.scatter)

    def hovered(self, scatter, points, event):
        if len(points) == 0:
            self.hide()
            return
        point = points[0]
        if point.data() is None:
            self.hide()
            return
        self.selected_scatter = scatter
        self.selection = (point.pos().x(), point.pos().y())
        self.label.setText(self.formatter(point.data()))
        self.label.adjustSize()
        self.label.show()
        self.label.raise_()
        self.position()

    def position(self, *args):
        if self.selection is None:
            return
        x, y = self.selection
        (left, right), (bottom, top) = self.plot.getViewBox().viewRange()
        self.lines[0].setLine(x, bottom, x, y)
        self.lines[1].setLine(left, y, x, y)
        for line in self.lines:
            line.show()
        viewport = self.plot.viewport()
        plot_position = self.plot.mapFromScene(self.plot.getViewBox().mapViewToScene(QtCore.QPointF(x, y)))
        position = viewport.mapFromGlobal(self.plot.mapToGlobal(plot_position))
        width, height = self.label.width(), self.label.height()
        left = position.x() + 18
        if left + width > viewport.width() - 4:
            left = position.x() - width - 18
        top = position.y() - height - 18
        if top < 4:
            top = position.y() + 18
        self.label.move(max(4, min(left, viewport.width() - width - 4)),
            max(4, min(top, viewport.height() - height - 4)))

    def hide(self):
        self.selection = None
        self.selected_scatter = None
        self.label.hide()
        for line in self.lines:
            line.hide()

    def forget(self, curve):
        if self.selected_scatter is curve.scatter:
            self.hide()
        if curve.scatter in self.scatters:
            self.scatters.remove(curve.scatter)

    def restore_lines(self):
        self.hide()
        for line in self.lines:
            self.plot.addItem(line, ignoreBounds=True)

    def eventFilter(self, watched, event):
        if not isValid(self.plot):
            return False
        if watched is self.plot.viewport():
            if event.type() == QtCore.QEvent.Type.MouseMove:
                viewport_point = event.position().toPoint()
                plot_point = self.plot.viewport().mapTo(self.plot, viewport_point)
                scene_point = self.plot.mapToScene(plot_point)
                candidates = []
                for scatter in tuple(self.scatters):
                    if not isValid(scatter):
                        self.scatters.remove(scatter)
                        continue
                    for point in scatter.points():
                        point_scene = scatter.mapToScene(point.pos())
                        distance = math.hypot(point_scene.x() - scene_point.x(), point_scene.y() - scene_point.y())
                        if distance <= 12 and point.data() is not None:
                            candidates.append((distance, -scatter.zValue(), scatter, point))
                if candidates:
                    _, _, scatter, point = min(candidates, key=lambda item: (item[0], item[1]))
                    if self.selected_scatter is not scatter or self.selection != (point.pos().x(), point.pos().y()):
                        self.hovered(scatter, [point], None)
                else:
                    self.hide()
            elif event.type() in (QtCore.QEvent.Type.Leave, QtCore.QEvent.Type.Hide):
                self.hide()
        return super().eventFilter(watched, event)
