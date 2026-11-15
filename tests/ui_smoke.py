"""Offscreen end-to-end GUI check using only a temporary demo database."""

from pathlib import Path
import tempfile
import time

from PySide6 import QtGui, QtWidgets

from gsmbench import ui
from gsmbench import __release_date__, __version__


app = QtWidgets.QApplication([])
font_path = Path("C:/Windows/Fonts/segoeui.ttf")
if font_path.exists():
    QtGui.QFontDatabase.addApplicationFont(str(font_path))
    app.setFont(QtGui.QFont("Segoe UI", 10))
with tempfile.TemporaryDirectory() as directory:
    ui.ROOT = Path(directory)
    ui.SETTINGS = ui.ROOT / "settings.local.json"
    window = ui.Window()
    errors = []
    window.error = errors.append
    window.show()
    app.processEvents()
    assert window.resource.text() == "TCPIP0::172.16.236.32::5025::SOCKET"
    available = window.screen().availableGeometry()
    assert abs(window.frameGeometry().center().x() - available.center().x()) <= 2
    assert abs(window.frameGeometry().center().y() - available.center().y()) <= 2
    assert window.model.text() == "КД419"
    assert not window.windowIcon().isNull()
    assert not window.windowIcon().pixmap(32, 32).isNull()
    window.help_action.trigger()
    app.processEvents()
    assert window.help_dialog.isVisible()
    assert window.help_dialog.tabs.count() == 4
    window.help_dialog.grab().save(str(ui.ROOT / "help-preview.png"))
    window.help_dialog.close()
    window.about_action.trigger()
    app.processEvents()
    assert window.about_dialog.isVisible()
    about_text = " ".join(label.text() for label in window.about_dialog.findChildren(QtWidgets.QLabel))
    assert __version__ in about_text and __release_date__ in about_text
    window.about_dialog.grab().save(str(ui.ROOT / "about-preview.png"))
    window.about_dialog.close()
    for spacing in ("linear", "log"):
        window.spacing.setCurrentIndex(window.spacing.findData(spacing))
        window.generate_points()
        levels = [float(value) for value in window.levels.text().split(";")]
        assert len(levels) == 20
        assert levels[0] == 0.001 and levels[-1] == 11
        assert window.levels.toolTip().count("<br>") == 19
    window.start_measurement()
    deadline = time.monotonic() + 15
    while window.worker is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert window.worker is None, "Worker did not finish"
    assert not errors, errors
    assert len(window.runs) == 1
    assert window.runs[0]["state"] == "complete"
    assert len(window.runs[0]["points"]) == 20
    window.table.selectRow(0)
    app.processEvents()
    curve = window.plot.listDataItems()[0]
    x, y = curve.getData()
    original_x, original_y = x.copy(), y.copy()
    window.swap_axes_button.click()
    x, y = curve.getData()
    assert (x == original_y).all() and (y == original_x).all()
    assert window.plot.getAxis("bottom").labelUnits == "A"
    window.plot_selected()
    x, y = window.plot.listDataItems()[0].getData()
    assert (x == original_y).all() and (y == original_x).all()
    window.swap_axes_button.click()
    x, y = window.plot.listDataItems()[0].getData()
    assert (x == original_x).all() and (y == original_y).all()
    assert window.plot.getAxis("bottom").labelUnits == "V"
    Path(".scratch").mkdir(exist_ok=True)
    import shutil
    shutil.copyfile(ui.ROOT / "help-preview.png", ".scratch/help-preview.png")
    shutil.copyfile(ui.ROOT / "about-preview.png", ".scratch/about-preview.png")
    window.grab().save(".scratch/ui-preview.png")
    window.range_stop.setText("100")
    window.generate_points()
    assert float(window.levels.text().split(";")[-1]) == 100
    window.wire_mode.setCurrentIndex(1)
    window.terminals.setCurrentIndex(1)
    window.delay.setValue(1)
    window.sample.setText("D021")
    window.model.setText("КД419В")
    window.batch.setText("Новая партия")
    window.mode.setCurrentText("VISA LAN")
    window.swap_axes()
    window.controls.setCurrentIndex(1)
    window.result_tabs.setCurrentIndex(2)
    saved_settings = window.settings()
    window.close()
    restored = ui.Window()
    assert restored.settings() == saved_settings
    assert "REAR, 4" in restored.wiring.text()
    assert restored.plot.getAxis("bottom").labelUnits == "A"
    restored.close()
print("UI smoke passed: demo sweep -> graph -> database -> table")
