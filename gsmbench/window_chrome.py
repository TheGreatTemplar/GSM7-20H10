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
        for attribute, rgb in ((34, (49, 64, 85)), (35, (27, 37, 49)), (36, (203, 213, 225))):
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
