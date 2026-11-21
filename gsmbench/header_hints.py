"""Persistent, delayed help for individual table header sections."""

from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import isValid


class HeaderHints(QtCore.QObject):
    def __init__(self, header):
        super().__init__(header)
        self.header, self.column = header, -1
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(3000)
        self.timer.timeout.connect(self.show_hint)
        self.label = QtWidgets.QLabel(None,
            QtCore.Qt.WindowType.ToolTip | QtCore.Qt.WindowType.WindowTransparentForInput)
        self.label.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.label.setWordWrap(True)
        self.label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.label.setMaximumWidth(460)
        self.label.setStyleSheet('background:#263447; color:#f5f8fc; border:1px solid #8191a8; padding:7px')
        self.destroyed.connect(self.label.deleteLater)
        header.viewport().setMouseTracking(True)
        header.viewport().installEventFilter(self)
        header.installEventFilter(self)

    def hide_hint(self):
        self.timer.stop()
        self.column = -1
        self.label.hide()

    def track(self, position):
        column = self.header.logicalIndexAt(position)
        if column != self.column:
            self.hide_hint()
            self.column = column
            if column >= 0:
                self.timer.start()

    def show_hint(self):
        viewport = self.header.viewport()
        cursor = viewport.mapFromGlobal(QtGui.QCursor.pos())
        if (not self.header.isVisible() or not viewport.rect().contains(cursor)
                or self.header.logicalIndexAt(cursor) != self.column or self.column < 0):
            return
        text = self.header.model().headerData(self.column, self.header.orientation(), QtCore.Qt.ItemDataRole.ToolTipRole)
        if not text:
            return
        self.label.setText(str(text))
        self.label.adjustSize()
        position = viewport.mapToGlobal(QtCore.QPoint(cursor.x()+16, viewport.height()+8))
        screen = self.header.screen().availableGeometry()
        position.setX(max(screen.left()+4, min(position.x(), screen.right()-self.label.width()-4)))
        position.setY(max(screen.top()+4, min(position.y(), screen.bottom()-self.label.height()-4)))
        self.label.move(position)
        self.label.show()

    def eventFilter(self, watched, event):
        if not isValid(self.header):
            return False
        kind = event.type()
        if watched is self.header.viewport():
            if kind == QtCore.QEvent.Type.MouseMove:
                self.track(event.position().toPoint())
            elif kind == QtCore.QEvent.Type.Enter:
                self.track(watched.mapFromGlobal(QtGui.QCursor.pos()))
            elif kind in (QtCore.QEvent.Type.Leave, QtCore.QEvent.Type.Hide,
                    QtCore.QEvent.Type.MouseButtonPress, QtCore.QEvent.Type.Wheel):
                self.hide_hint()
            elif kind == QtCore.QEvent.Type.ToolTip:
                return True
        elif kind in (QtCore.QEvent.Type.Hide, QtCore.QEvent.Type.Resize):
            self.hide_hint()
        return super().eventFilter(watched, event)
