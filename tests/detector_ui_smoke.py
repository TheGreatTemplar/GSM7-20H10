from pathlib import Path
import tempfile
import time
from unittest.mock import patch
from unittest.mock import Mock

from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from gsmbench import ui


app = QtWidgets.QApplication([])
QtGui.QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
app.setFont(QtGui.QFont("Segoe UI", 10))


def wait(panel):
    deadline = time.monotonic() + 15
    while panel.worker is not None and time.monotonic() < deadline:
        QtTest.QTest.qWait(10)
        time.sleep(0.001)
    assert panel.worker is None


with tempfile.TemporaryDirectory() as directory:
    ui.ROOT = Path(directory)
    ui.SETTINGS = ui.ROOT / "settings.local.json"
    main = ui.Window()
    main.show()
    main.resize(1380, 900)
    main.show_detector()
    panel = main.detector_window
    assert main.workspaces.currentWidget() is main.detector_page
    assert panel.fields["com_port"].text() == "COM3"
    assert isinstance(panel.fields["high_impedance"], QtWidgets.QComboBox)
    assert panel.fields["high_impedance"].currentData() is True
    panel.fields["high_impedance"].setCurrentIndex(0)
    assert panel.settings()["high_impedance"] is False
    panel.fields["nplc"].setCurrentIndex(panel.fields["nplc"].findData(100))
    panel.fields["meter_kind"].setCurrentIndex(panel.fields["meter_kind"].findData("dmm6500"))
    assert panel.fields["dmm_host"].text() == "172.16.236.45"
    assert panel.fields["nplc"].currentData() == 10
    assert not panel.fields["nplc"].model().item(panel.fields["nplc"].findData(100)).isEnabled()
    panel.fields["meter_kind"].setCurrentIndex(panel.fields["meter_kind"].findData("hp34401a"))
    panel.wiring.setChecked(True)
    panel.fields["sample"].setText("LODET_UI")
    panel.fields["frequency_mhz"].setValue(7.1)
    assert panel.wiring.isChecked()
    panel.fields["attenuation_db"].setValue(3)
    assert not panel.wiring.isChecked()
    panel.fields["attenuation_db"].setValue(0)
    panel.fields["start_dbm"].setValue(-20)
    panel.fields["stop_dbm"].setValue(-16)
    panel.fields["settling_s"].setValue(0)
    with patch("gsmbench.detector_ui.Hp34401a", side_effect=AssertionError("Demo connected to meter")), \
            patch("gsmbench.detector.Ssg3000x", side_effect=AssertionError("Demo enabled RF")):
        panel.launch("series")
        assert not main.workspaces.isTabEnabled(1)
        wait(panel)
    assert panel.last_error is None, panel.status.text()
    assert panel.current["state"] == "complete" and len(panel.current["points"]) == 5
    assert panel.current["rf_off_confirmed"] and not panel.unsafe_outputs
    assert main.workspaces.isTabEnabled(1)
    assert not (ui.ROOT / "data" / "lo-detectors.db").exists()
    app.processEvents()
    plot = panel.plot
    point = panel.current["points"][-1]
    position = plot.mapFromScene(plot.getViewBox().mapViewToScene(
        QtCore.QPointF(point["input_dbm"], point["voltage_v"])))
    QtTest.QTest.mouseMove(plot.viewport(), position)
    QtTest.QTest.qWait(100)
    assert plot.hover_label.isVisible(), "No detector tooltip on actual mouse hover"
    assert "Точка 5/5" in plot.hover_label.text() and "-16.000 dBm" in plot.hover_label.text()
    assert "7.1 МГц" in plot.hover_label.text() and "U =" in plot.hover_label.text()
    assert all(line.isVisible() for line in plot.hover_lines)
    assert not plot.hover_label.geometry().adjusted(-4, -4, 4, 4).contains(position)
    QtTest.QTest.qWait(2500)
    assert plot.hover_label.isVisible(), "Detector tooltip expired while on point"
    Path(".scratch").mkdir(exist_ok=True)
    main.grab().save(".scratch/detector-hover-preview.png")
    app.sendEvent(plot.viewport(), QtCore.QEvent(QtCore.QEvent.Type.Leave))
    assert not plot.hover_label.isVisible()
    assert not any(line.isVisible() for line in plot.hover_lines)
    panel.convert_voltage.setValue(panel.current["points"][2]["voltage_v"])
    panel.convert()
    assert "-18.000 dBm" in panel.convert_result.text(), panel.convert_result.text()
    for filename, callback in (("detector.csv", panel.export_csv), ("detector.png", panel.export_png), ("detector.json", panel.export_json)):
        path = ui.ROOT / filename
        with patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=(str(path), "")):
            callback()
        assert path.stat().st_size > 100
    assert QtGui.QImage(str(ui.ROOT / "detector.png")).width() == 2400
    panel.search.setText("not present")
    assert panel.archive.isRowHidden(0)
    panel.search.clear()
    panel.archive.selectRow(0)
    app.processEvents()
    Path(".scratch").mkdir(exist_ok=True)
    main.grab().save(".scratch/detector-ui-preview.png")
    panel.fields["demo"].setChecked(False)
    panel.launch("series")
    assert panel.worker is None and "подключите" in panel.status.text().casefold()
    panel.wiring.setChecked(True)
    with patch.dict("gsmbench.detector_ui.SOURCE_FACTORIES", {"akip": Mock(side_effect=TimeoutError("could not connect: -1073807339"))}):
        panel.launch("series")
        wait(panel)
    assert not panel.unsafe_outputs
    assert panel.form_widget.isEnabled() and main.workspaces.isTabEnabled(1)
    panel.unsafe_outputs = True
    panel.set_busy(True)
    assert not panel.form_widget.isEnabled() and panel.manual_off_button.isVisible()
    panel.manual_off_button.click()
    assert not panel.unsafe_outputs and panel.form_widget.isEnabled()
    panel.fields["demo"].setChecked(True)
    panel.fields["settling_s"].setValue(1)
    panel.launch("series")
    panel.stop()
    wait(panel)
    assert not panel.unsafe_outputs
    saved = panel.settings()
    main.close()
    restored = ui.Window()
    restored.show_detector()
    assert restored.detector_window.settings() == saved
    assert not restored.detector_window.wiring.isChecked()
    restored.close()
print("Detector UI smoke passed")
