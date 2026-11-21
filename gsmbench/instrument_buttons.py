"""Compact instrument connection actions with explicit accessible names."""

from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtSvg import QSvgRenderer
from shiboken6 import isValid


class ConnectionTestButton(QtWidgets.QPushButton):
    COLORS = dict(idle='#d8e2ef', busy='#f4bd61', ok='#65d6a4', error='#ff877e', demo='#8db9ed')
    STATES = dict(idle='Связь не проверена.', busy='Проверка выполняется…',
        ok='Последняя проверка связи успешна. Непрерывный контроль не выполняется.',
        error='Ошибка последней проверки связи.', demo='Демо: реальная связь с прибором не проверялась.')

    def __init__(self, title, callback, description):
        super().__init__()
        self.title, self.description, self.revision = title, description, 0
        self.setIconSize(QtCore.QSize(20, 20))
        self.setFixedSize(32, 28)
        self.setAccessibleName(title)
        self.set_state('idle')
        self.clicked.connect(callback)

    def set_state(self, state, detail=''):
        color = self.COLORS[state]
        self.setProperty('connectionState', state)
        svg = (Path(__file__).resolve().parent.parent / 'assets' / 'test-connection.svg').read_bytes()
        renderer = QSvgRenderer(svg.replace(b'#d8e2ef', color.encode('ascii')))
        icon = QtGui.QIcon()
        for ratio in (1, 2, 3):
            pixmap = QtGui.QPixmap(20*ratio, 20*ratio)
            pixmap.fill(QtCore.Qt.GlobalColor.transparent)
            painter = QtGui.QPainter(pixmap)
            renderer.render(painter)
            painter.end()
            pixmap.setDevicePixelRatio(ratio)
            icon.addPixmap(pixmap)
            icon.addPixmap(pixmap, QtGui.QIcon.Mode.Disabled)
        self.setIcon(icon)
        border = '#405570' if state == 'idle' else color
        self.setStyleSheet('''
            QPushButton
            {
                padding: 3px;
                border: 1px solid BORDER_COLOR;
            }
        '''.replace('BORDER_COLOR', border))
        status = self.STATES[state] + ('\n' + str(detail) if detail else '')
        self.setToolTip('\n'.join(part for part in (self.title, status, self.description) if part))
        self.setAccessibleDescription(status + '\n' + self.description)

    def reset(self, *args):
        self.revision += 1
        self.set_state('idle')

    def bind_endpoint(self, *widgets):
        for widget in widgets:
            signal = (widget.toggled if isinstance(widget, QtWidgets.QCheckBox) else
                widget.currentTextChanged if isinstance(widget, QtWidgets.QComboBox) else
                widget.valueChanged if isinstance(widget, QtWidgets.QAbstractSpinBox) else widget.textChanged)
            signal.connect(self.reset)

    def watch(self, worker, demo=False):
        self.revision += 1
        revision = self.revision
        result = dict(error=None)
        self.set_state('busy')

        def failed(message):
            result['error'] = str(message)
            if isValid(self) and self.revision == revision:
                self.set_state('error', message)

        def finished():
            if not isValid(self) or self.revision != revision or result['error'] is not None:
                return
            if getattr(worker, 'stop', None) is not None and worker.stop.is_set():
                self.set_state('idle', 'Проверка остановлена.')
            else:
                self.set_state('demo' if demo else 'ok')

        worker.failed.connect(failed)
        worker.finished.connect(finished)


def connection_test_button(title, callback, description=''):
    return ConnectionTestButton(title, callback, description)
