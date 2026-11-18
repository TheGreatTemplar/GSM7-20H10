"""Archive plot interactions with synthetic spectra and no hardware access."""

import copy
from pathlib import Path
import tempfile
from unittest.mock import patch

from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from gsmbench.rf_charts import ArchiveCharts
from gsmbench.ui import STYLE
from test_rf_charts import sample


app = QtWidgets.QApplication([])
app.setStyle("Fusion")
QtGui.QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
app.setFont(QtGui.QFont("Segoe UI", 10))
spectra = []
for frequency in (14e6, 15e6, 16e6):
    for power in (-30, -20, -10):
        spectrum = sample(frequency)
        spectrum["context"]["rf_actual_dbm"] = power
        spectrum["context"]["position"] = "center" if frequency == 15e6 else "edge"
        spectrum["averaging"] = dict(marker_std_db=0.03, count=5, method="linear_power")
        spectra.append(spectrum)
repeat = copy.deepcopy(spectra[0])
repeat["id"] = "repeat"
repeat["power_dbm"] = [value + 0.1 for value in repeat["power_dbm"]]
spectra.append(repeat)
calls = []


def provider(selected):
    calls.append(selected)
    return spectra[:1] if selected else spectra


chart = ArchiveCharts(provider)
chart.setStyleSheet(STYLE)
chart.resize(1150, 850)
chart.show()
QtTest.QTest.qWait(100)
assert chart.plot.backgroundBrush().color().name() == "#101923"
assert chart.plot.getAxis("bottom").labelText == chart.x_axis.currentText()
assert chart.plot.getAxis("left").labelText == chart.y_axis.currentText()
assert not chart.plot.getViewBox().menuEnabled()
chart.build_chart()
assert calls == [False]
assert len(chart.groups) == 3
chart.x_axis.setCurrentIndex(chart.x_axis.findData("rf_level"))
assert calls == [False], "Changing axes reloaded the archive"
assert len(chart.groups) == 3
assert chart.groups[0]["points"][0]["x"] == -30
chart.y_axis.setCurrentIndex(chart.y_axis.findData("output"))
assert chart.groups[0]["points"][0]["y"] < 0
assert chart.plot.getAxis("left").labelText == chart.y_axis.currentText()
chart.centers_only.setChecked(True)
assert len(chart.groups) == 1
chart.centers_only.setChecked(False)
chart.show_spread.setChecked(False)
assert len(chart.groups) == 3 and calls == [False]
chart.x_axis.setCurrentIndex(0)
chart.y_axis.setCurrentIndex(0)
chart.show_spread.setChecked(True)
chart.reset_scale()
QtTest.QTest.qWait(100)
curve = chart.plot.listDataItems()[0]
x, y = curve.getData()
position = chart.plot.mapFromScene(chart.plot.getViewBox().mapViewToScene(QtCore.QPointF(x[1], y[1])))
QtTest.QTest.mouseMove(chart.plot.viewport(), position)
QtTest.QTest.qWait(120)
assert chart.point_hover.label.isVisible(), "Point tooltip did not appear"
assert "RF, МГц: 15" in chart.point_hover.label.text()
assert "Точка 2/3" in chart.point_hover.label.text()
assert "Разность генератор → RSA, дБ" in chart.point_hover.label.text()
assert all(line.isVisible() for line in chart.point_hover.lines)
assert not chart.point_hover.label.geometry().adjusted(-3, -3, 3, 3).contains(position)
QtTest.QTest.qWait(2300)
assert chart.point_hover.label.isVisible(), "Point tooltip expired"
output = Path(".scratch")
output.mkdir(exist_ok=True)
chart.grab().save(str(output / "archive-chart-point-hover.png"))
app.sendEvent(chart.plot.viewport(), QtCore.QEvent(QtCore.QEvent.Type.Leave))
assert not chart.point_hover.label.isVisible()
menu = chart.create_plot_menu()
assert [action.text() for action in menu.actions()] == ["Масштаб по данным",
    "Показывать разброс повторов / развёрток", "Сохранить PNG…", "Экспорт точек CSV…"]
menu.actions()[1].trigger()
assert not chart.show_spread.isChecked() and calls == [False]
chart.plot.setRange(xRange=(0, 1), yRange=(0, 1), disableAutoRange=True)
menu.actions()[0].trigger()
assert all(chart.plot.getViewBox().autoRangeEnabled())
with patch.object(chart, "create_plot_menu", return_value=menu) as opened:
    QtCore.QTimer.singleShot(100, menu.close)
    event = QtGui.QContextMenuEvent(QtGui.QContextMenuEvent.Reason.Mouse,
        chart.plot.viewport().rect().center(), chart.plot.viewport().mapToGlobal(chart.plot.viewport().rect().center()))
    app.sendEvent(chart.plot.viewport(), event)
    assert opened.call_count == 1, "Right click did not open the program menu"
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory)
    with patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=(str(path / "chart.png"), "")):
        chart.export_png()
    assert QtGui.QImage(str(path / "chart.png")).width() == 2400, chart.status.text()
    with patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=(str(path / "chart.csv"), "")):
        chart.export_csv()
    assert "RF, МГц" in (path / "chart.csv").read_text(encoding="utf-8-sig")
chart.scope.setCurrentIndex(1)
assert chart.spectra is None and not chart.groups
chart.y_axis.setCurrentIndex(2)
assert calls == [False]
chart.build_chart()
assert calls == [False, True]
assert sum(len(group["points"]) for group in chart.groups) == 1
chart.invalidate()
assert not chart.groups and not chart.point_hover.label.isVisible()
chart.close()
print("Archive charts UI passed: theme, labels, cached redraw, point hover, program menu, PNG/CSV, invalidation")
