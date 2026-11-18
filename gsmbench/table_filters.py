"""Visible clickable filter headers shared by component and RF archives."""

from PySide6 import QtCore, QtGui, QtWidgets


class FilterHeader(QtWidgets.QHeaderView):
    def __init__(self, parent):
        super().__init__(QtCore.Qt.Orientation.Horizontal, parent)
        self.active_filters = set()
        self.setSectionsClickable(True)

    def set_active_filters(self, columns):
        self.active_filters = set(columns)
        self.viewport().update()

    def paintSection(self, painter, rect, logical_index):
        super().paintSection(painter, rect, logical_index)
        if not rect.isValid():
            return
        active = logical_index in self.active_filters
        painter.save()
        painter.fillRect(rect.adjusted(1, 1, -1, -1), QtGui.QColor("#3c4757" if active else "#252e3b"))
        painter.setPen(QtGui.QColor("#eef1f5" if active else "#c4cbd4"))
        font = painter.font()
        font.setBold(active)
        painter.setFont(font)
        text = self.model().headerData(logical_index, self.orientation(), QtCore.Qt.ItemDataRole.DisplayRole)
        painter.drawText(rect.adjusted(6, 0, -5, 0), QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter, str(text or ""))
        painter.restore()
