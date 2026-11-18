"""Headless end-to-end calibration UI; all hardware constructors are forbidden."""

from pathlib import Path
import tempfile
import time
from unittest.mock import patch

from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from gsmbench import ui


app = QtWidgets.QApplication([])
QtGui.QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
app.setFont(QtGui.QFont("Segoe UI", 10))


def wait(panel):
    deadline = time.monotonic() + 20
    while panel.worker is not None and time.monotonic() < deadline:
        QtTest.QTest.qWait(10)
    assert panel.worker is None


with tempfile.TemporaryDirectory() as directory:
    ui.ROOT = Path(directory)
    ui.SETTINGS = ui.ROOT / "settings.local.json"
    main = ui.Window()
    main.resize(1600, 1000)
    main.show()
    main.show_rsa()
    bench = main.rsa_window
    app.processEvents()
    main.resize(1600, 1000)
    bench.demo.setChecked(True)
    bench.settling_s.setValue(0)
    panel = bench.two_tone
    bench.views.setCurrentWidget(panel)
    panel.pages.setCurrentIndex(1)
    calibration = panel.calibration
    assert calibration.fields["nrp_averages"].currentData() == 128
    assert calibration.fields["nrp_settling_s"].value() == 1
    calibration.fields["nrp_settling_s"].setValue(0)
    assert calibration.fields["frequency_start"].maximum() == 200
    assert calibration.fields["frequency_stop"].maximum() == 200
    assert panel.fields["f1_mhz"].maximum() == 200
    assert "Схема подключения" in calibration.instructions_button.text()
    calibration.fields["frequency_stop"].setValue(13)
    assert calibration.fields["frequency_stop"].property("inputError")
    assert calibration.status.property("hasError")
    calibration.fields["frequency_stop"].setValue(14)
    assert not calibration.fields["frequency_stop"].property("inputError")
    assert not calibration.status.property("hasError")
    panel.mode.setCurrentIndex(panel.mode.findData("mixer"))
    for key in ("pad_rating", "max_w", "harmonics"):
        assert not panel.form.isRowVisible(panel.fields[key])
    panel.mode.setCurrentIndex(panel.mode.findData("pa"))
    for key in ("pad_rating", "max_w", "harmonics"):
        assert panel.form.isRowVisible(panel.fields[key])
    calibration.fields["loss1"].setValue(16)
    calibration.fields["loss2"].setValue(16)
    assert calibration.plan().loss1_db == 16 and calibration.plan().loss2_db == 16
    assert panel.fields["loss1"].value() == 16 and panel.fields["loss2"].value() == 16
    calibration.fields["loss1"].setValue(6)
    calibration.fields["loss2"].setValue(6)
    calibration.fields["last"].setValue(-24)
    calibration.fields["samples"].setValue(1)
    with patch("gsmbench.source_calibration.Smu200a", side_effect=AssertionError("Demo connected to SMU")), \
            patch("gsmbench.source_calibration.Rsa6265", side_effect=AssertionError("Demo connected to RSA")), \
            patch("gsmbench.source_calibration.Nrp18t", side_effect=AssertionError("Demo connected to NRP")):
        for method in ("rsa", "nrp"):
            calibration.method.setCurrentIndex(calibration.method.findData(method))
            assert calibration.form.isRowVisible(calibration.fields["spacing"]) == (method == "rsa")
            if method == "nrp":
                assert calibration.plan().f1_hz == calibration.plan().f2_hz
            calibration.start_button.click()
            assert panel.worker is not None, calibration.status.text()
            assert not calibration.start_button.isEnabled()
            wait(panel)
            assert not panel.unsafe_outputs
            assert calibration.start_button.isEnabled()
            assert len(calibration.available) == (1 if method == "rsa" else 2)
        calibration.fields["frequency_stop"].setValue(14.002)
        calibration.fields["frequency_step"].setValue(0.002)
        assert "2 частот" in calibration.plan_summary.text()
        calibration.start_button.click()
        wait(panel)
        assert len(calibration.available) == 4
        assert "частот 2/2" in calibration.status.text()
        assert calibration.table.rowCount() == 6
        assert "Частота" in calibration.table.horizontalHeaderItem(0).text()
        assert {calibration.table.item(row, 0).text() for row in range(6)} == {"14.000000", "14.002000"}
        assert len(calibration.plot.listDataItems()) == 4
        names = [curve.name() for curve in calibration.plot.listDataItems()]
        assert any("14.002 МГц" in name for name in names)
        calibration.frequency_filter.setCurrentIndex(1)
        assert calibration.table.rowCount() == 3
        assert len(calibration.plot.listDataItems()) == 2
        calibration.frequency_filter.setCurrentIndex(0)
        assert calibration.table.rowCount() == 6
        calibration.profiles.setCurrentIndex(0)
        calibration.use_profile.setChecked(True)
        panel.pages.setCurrentIndex(0)
        panel.analyzer_preset()
        panel.start_button.click()
        assert panel.worker is not None, panel.status.text()
        wait(panel)
        assert panel.points, panel.status.text()
        spectra = bench.store().load()
        source_profile = calibration.profile_for_plan(panel.plans()[0])
        assert spectra[0]["context"]["source_calibration"]["id"] == source_profile["id"]
    panel.pages.setCurrentIndex(1)
    app.processEvents()
    old_sizes = calibration.splitter.sizes()
    calibration.splitter.moveSplitter(old_sizes[0] - 35, 1)
    assert calibration.splitter.sizes() != old_sizes
    assert calibration.start_button.isVisible() and calibration.plot.isVisible()
    assert calibration.fields["first"].width() <= 140
    assert calibration.method.width() <= 280
    resource = calibration.fields["nrp_resource"]
    assert resource.width() >= resource.fontMetrics().horizontalAdvance(resource.text()) + 20
    calibration.plot.setRange(xRange=(0, 1), yRange=(0, 1), disableAutoRange=True)
    calibration.reset_scale_button.click()
    app.processEvents()
    assert calibration.plot.getViewBox().viewRange()[0][1] < 0
    assert all(calibration.plot.getViewBox().autoRangeEnabled())
    calibration.frequency_filter.setCurrentIndex(1)
    calibration.reset_scale()
    app.processEvents()
    curve = next(curve for key, curve in calibration.plot_curves.items() if key[1] == 0)
    x, y = curve.getData()
    position = calibration.plot.mapFromScene(calibration.plot.getViewBox().mapViewToScene(QtCore.QPointF(x[1], y[1])))
    QtTest.QTest.mouseMove(calibration.plot.viewport(), position)
    QtTest.QTest.qWait(100)
    assert calibration.point_hover.label.isVisible(), "Calibration point hint did not appear"
    assert "14 МГц" in calibration.point_hover.label.text()
    assert "Поправка:" in calibration.point_hover.label.text()
    assert "Измерено:" in calibration.point_hover.label.text()
    assert all(line.isVisible() for line in calibration.point_hover.lines)
    assert not calibration.point_hover.label.geometry().adjusted(-3, -3, 3, 3).contains(position)
    QtTest.QTest.qWait(2500)
    assert calibration.point_hover.label.isVisible(), "Point hint expired"
    Path(".scratch").mkdir(exist_ok=True)
    main.grab().save(".scratch/calibration-point-hover.png")
    app.sendEvent(calibration.plot.viewport(), QtCore.QEvent(QtCore.QEvent.Type.Leave))
    assert not calibration.point_hover.label.isVisible()
    calibration.frequency_filter.setCurrentIndex(0)
    Path(".scratch").mkdir(exist_ok=True)
    main.grab().save(".scratch/source-calibration-preview.png")
    calibration.results_tabs.setCurrentWidget(calibration.table)
    app.processEvents()
    main.grab().save(".scratch/calibration-table-preview.png")
    calibration.results_tabs.setCurrentWidget(calibration.plot)
    calibration.fields["frequency_stop"].setValue(13)
    app.processEvents()
    assert calibration.status.property("hasError")
    assert calibration.fields["frequency_stop"].property("inputError")
    assert calibration.plan_summary.isVisible()
    main.grab().save(".scratch/calibration-validation-preview.png")
    calibration.fields["frequency_stop"].setValue(14.002)
    calibration.fields["loss1"].setValue(16)
    calibration.fields["loss2"].setValue(16)
    saved = panel.settings()
    main.close()
    restored = ui.Window()
    restored.show_rsa()
    assert restored.rsa_window.two_tone.settings() == saved
    restored.close()
print("Source calibration UI smoke passed")
