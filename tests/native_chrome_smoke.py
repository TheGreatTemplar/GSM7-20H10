"""Check DWM on hidden native windows; never opens the user's running app."""

import ctypes
from PySide6 import QtWidgets
from gsmbench.window_chrome import install_window_chrome, apply_dark_title_bar

app = QtWidgets.QApplication([])
install_window_chrome()
window = QtWidgets.QMainWindow()
dialog = QtWidgets.QDialog(window)
for widget in (window, dialog):
    assert apply_dark_title_bar(widget)
    getter = ctypes.windll.dwmapi.DwmGetWindowAttribute
    getter.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
    getter.restype = ctypes.c_long
    value = ctypes.c_int()
    status = getter(int(widget.winId()), 20, ctypes.byref(value), ctypes.sizeof(value))
    if status == 0:
        assert value.value == 1
    assert widget.property("darkTitleBarApplied")
    widget.close()
print("Native dark title bar accepted by DWM for main window and dialog")
