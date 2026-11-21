"""Match native Windows title bars to the application's dark client area.

Uses the documented DWM attributes; unsupported color overrides are optional.
https://learn.microsoft.com/windows/win32/api/dwmapi/ne-dwmapi-dwmwindowattribute
"""

import ctypes
import sys
import weakref

from PySide6 import QtCore, QtWidgets
from shiboken6 import isValid


def apply_dark_title_bar(widget):
    if sys.platform != "win32" or QtWidgets.QApplication.platformName() != "windows":
        return False
    try:
        setter = ctypes.windll.dwmapi.DwmSetWindowAttribute
        setter.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
        setter.restype = ctypes.c_long
        hwnd = int(widget.winId())
        dark = ctypes.c_int(1)
        result = setter(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
        if result != 0:
            return False
        # Caption/border colors are supported on Windows 11. Windows 10 uses
        # its native dark caption when these optional attributes are unavailable.
        for attribute, rgb in ((34, (49, 64, 85)), (35, (36, 48, 64)), (36, (216, 226, 239))):
            red, green, blue = rgb
            color = ctypes.c_uint(red | green << 8 | blue << 16)
            setter(hwnd, attribute, ctypes.byref(color), ctypes.sizeof(color))
        widget.setProperty("darkTitleBarApplied", True)
        return True
    except (AttributeError, OSError):
        return False


class WindowChrome(QtCore.QObject):
    def eventFilter(self, watched, event):
        if (event.type() in (QtCore.QEvent.Type.Show, QtCore.QEvent.Type.WinIdChange)
                and isinstance(watched, QtWidgets.QWidget) and isValid(watched) and watched.isWindow()
                and watched.windowType() in (QtCore.Qt.WindowType.Window, QtCore.Qt.WindowType.Dialog, QtCore.Qt.WindowType.Tool)):
            target = weakref.ref(watched)

            def apply():
                widget = target()
                if widget is not None and isValid(widget):
                    apply_dark_title_bar(widget)

            QtCore.QTimer.singleShot(0, apply)
        return super().eventFilter(watched, event)


def install_window_chrome():
    app = QtWidgets.QApplication.instance()
    if app is not None and not hasattr(app, "window_chrome"):
        app.window_chrome = WindowChrome(app)
        app.installEventFilter(app.window_chrome)


class TitleBar(QtWidgets.QWidget):
    """Colored caption for Windows 10, where DWM caption colors are unsupported."""
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setObjectName("workbenchTitleBar")
        self.setStyleSheet("""
            QWidget#workbenchTitleBar, QWidget#workbenchTitleBar QLabel
            {
                background: #243040;
                color: #d8e2ef;
            }
            QWidget#workbenchTitleBar QToolButton
            {
                background: #243040;
                border: 0;
                padding: 5px 12px;
            }
            QWidget#workbenchTitleBar QToolButton:hover { background: #405570; }
            QToolButton#captionClose:hover { background: #a33b46; }
        """)
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(7, 0, 0, 0)
        icon = QtWidgets.QLabel()
        icon.setPixmap(window.windowIcon().pixmap(20, 20))
        icon.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(icon)
        title = QtWidgets.QLabel(window.windowTitle())
        title.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(title, 1)
        window.windowTitleChanged.connect(title.setText)
        for text, tip, action in (("—", "Свернуть", window.showMinimized),
                ("□", "Развернуть / восстановить", self.toggle_maximized), ("×", "Закрыть", window.close)):
            button = QtWidgets.QToolButton()
            button.setText(text)
            button.setToolTip(tip)
            button.setAccessibleName(tip)
            if text == "×":
                button.setObjectName("captionClose")
            button.clicked.connect(action)
            row.addWidget(button)

    def toggle_maximized(self):
        self.window.showNormal() if self.window.isMaximized() else self.window.showMaximized()

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self.window.windowHandle():
            self.window.windowHandle().startSystemMove()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.toggle_maximized()


def install_colored_caption(window):
    if sys.platform != "win32" or sys.getwindowsversion().build >= 22000:
        return
    menu = window.menuBar()
    window.setWindowFlag(QtCore.Qt.WindowType.FramelessWindowHint, True)
    container = QtWidgets.QWidget(window)
    layout = QtWidgets.QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    window.colored_title_bar = TitleBar(window)
    # Reparent before replacing QMainWindow's menu widget.
    menu.setParent(container)
    layout.addWidget(window.colored_title_bar)
    layout.addWidget(menu)
    window.setMenuWidget(container)


def caption_native_event(window, message):
    if sys.platform != "win32" or not hasattr(window, "colored_title_bar") or window.isMaximized():
        return None
    from ctypes import wintypes
    msg = ctypes.cast(int(message), ctypes.POINTER(wintypes.MSG)).contents
    if msg.message != 0x0084:  # WM_NCHITTEST: preserve native edge/corner resizing.
        return None
    x = ctypes.c_short(msg.lParam & 0xffff).value
    y = ctypes.c_short((msg.lParam >> 16) & 0xffff).value
    rect = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(wintypes.HWND(int(window.winId())), ctypes.byref(rect))
    border = max(5, round(6 * window.devicePixelRatioF()))
    left, right = x < rect.left + border, x >= rect.right - border
    top, bottom = y < rect.top + border, y >= rect.bottom - border
    hit = (13 if left else 14 if right else 12) if top else (16 if left else 17 if right else 15) if bottom else 10 if left else 11 if right else None
    return (True, hit) if hit is not None else None
