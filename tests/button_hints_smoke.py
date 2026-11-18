"""Exercise the real hover timer with a moving cursor; no application I/O."""

from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from gsmbench.button_hints import install_button_hints
from gsmbench.ui import STYLE

app = QtWidgets.QApplication([])
QtGui.QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
app.setFont(QtGui.QFont("Segoe UI", 10))
install_button_hints()
window = QtWidgets.QWidget()
window.setStyleSheet(STYLE)
layout = QtWidgets.QVBoxLayout(window)
first = QtWidgets.QPushButton("Сбросить масштаб")
second = QtWidgets.QPushButton("Применить настройки RSA")
layout.addWidget(first)
layout.addWidget(second)
window.resize(400, 160)
window.show()
app.processEvents()
QtTest.QTest.mouseMove(window, QtCore.QPoint(2, 2))
app.processEvents()
QtTest.QTest.mouseMove(first, QtCore.QPoint(20, first.height() // 2))
elapsed = QtCore.QElapsedTimer()
elapsed.start()
while elapsed.elapsed() < 2650:
    QtTest.QTest.mouseMove(first, QtCore.QPoint(20 + elapsed.elapsed() % 120, first.height() // 2))
    QtTest.QTest.qWait(100)
assert not app.button_hints.label.isVisible(), "Hint appeared before three seconds"
QtTest.QTest.qWait(450)
assert app.button_hints.label.isVisible(), "Moving inside the button restarted the delay"
assert "обе оси" in app.button_hints.label.text()
QtTest.QTest.qWait(2200)
assert app.button_hints.label.isVisible(), "Stationary button hint expired"
QtTest.QTest.mouseMove(second, QtCore.QPoint(30, second.height() // 2))
app.processEvents()
assert not app.button_hints.label.isVisible()
assert app.button_hints.timer.remainingTime() > 2800
QtTest.QTest.mouseMove(window, QtCore.QPoint(2, 2))
app.processEvents()
assert not app.button_hints.timer.isActive()
window.close()
print("Button hints: delayed 3 seconds, moving cursor accepted, persistent until leave")
