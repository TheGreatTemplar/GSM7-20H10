"""Offscreen RSA window check; temporary databases and synthetic RF only."""

from pathlib import Path
import copy
import tempfile
import time
import uuid
from unittest.mock import patch

from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from gsmbench import ui


app = QtWidgets.QApplication([])
font = Path("C:/Windows/Fonts/segoeui.ttf")
if font.exists():
    QtGui.QFontDatabase.addApplicationFont(str(font))
    app.setFont(QtGui.QFont("Segoe UI", 10))


def wait_worker(window):
    deadline = time.monotonic() + 15
    while window.worker is not None and time.monotonic() < deadline:
        QtTest.QTest.qWait(10)
    assert window.worker is None, "RSA worker did not finish"


with tempfile.TemporaryDirectory() as directory:
    ui.ROOT = Path(directory)
    ui.SETTINGS = ui.ROOT / "settings.local.json"
    main = ui.Window()
    main.show()
    main.rsa_action.trigger()
    window = main.rsa_window
    assert window.measure_button.text() == "Измерить"
    with patch.object(window, "launch") as launch:
        window.measure_button.click()
        launch.assert_called_once_with("single")
    window.views.setCurrentWidget(window.two_tone)
    with patch.object(window.two_tone, "start") as start:
        window.measure_button.click()
        start.assert_called_once()
    window.views.setCurrentIndex(0)
    assert main.workspaces.currentWidget() is main.mixers_page
    assert not window.isWindow(), "Mixer controls opened a separate native window"
    assert [window.setup_tabs.tabText(i) for i in range(3)] == ["Измерения", "Генератор", "Анализатор"]
    for method, source, output in (("lo_rf", "LO", "RF"), ("rf_lo", "RF", "LO"),
                                   ("if_rf", "IF", "RF"), ("if_lo", "IF", "LO")):
        window.method.setCurrentIndex(window.method.findData(method))
        assert f"{source} → {output}" in window.wiring.text()
    assert window.host.text() == "172.16.236.90"
    assert window.store().path == ui.ROOT / "data" / "mixers.db"
    assert not window.store().path.exists()
    window.demo.setChecked(True)
    assert window.store().path == ui.ROOT / "data" / "mixers-demo.db"
    window.pin_known.setChecked(True)
    window.path_known.setChecked(True)
    window.method.setCurrentIndex(window.method.findData("conversion"))
    with patch("gsmbench.rsa_ui.Rsa6265", side_effect=AssertionError("Demo connected to real hardware")):
        window.launch("single")
        wait_worker(window)
    assert window.last_error is None, window.last_error
    assert len(window.spectra) == 1
    assert window.spectra[0]["averaging"]["count"] == 5
    assert len(window.spectra[0]["averaging"]["raw_sweeps"]) == 5
    app.processEvents()
    before_sizes = window.archive_splitter.sizes()
    window.archive_splitter.moveSplitter(120, 1)
    assert window.archive_splitter.sizes() != before_sizes
    window.panels_splitter.moveSplitter(320, 1)
    assert "7.000" in window.result.text(), window.result.text()
    assert not (ui.ROOT / "data" / "mixers.db").exists()
    window.if_hz.setCurrentIndex(window.if_hz.findData(8192000))
    window.center_on_tone()
    assert window.center_mhz.value() == 8.192
    assert window.context()["lo_hz"] == 22192000
    window.series_count.setValue(3)
    window.interval_s.setValue(0)
    window.launch("series")
    wait_worker(window)
    assert window.saved_count == 3 and len(window.spectra) == 4
    window.search.setText("not present")
    assert all(window.table.isRowHidden(row) for row in range(4))
    window.search.clear()
    window.table.selectRow(0)
    assert window.current["context"]["if_hz"] == 8192000
    app.processEvents()
    position = window.plot.getViewBox().mapViewToScene(QtCore.QPointF(8192000, -27))
    window.move_marker(position)
    assert window.cross_x.isVisible() and "8.192000" in window.marker_text.text(), window.marker_text.text()
    assert window.marker_label.isVisible()
    assert f"Точка: {window.marker_index + 1} / 801" in window.marker_label.text()
    assert "8.192000" in window.marker_label.text() and "-27.000 dBm" in window.marker_label.text()
    QtTest.QTest.qWait(3100)
    assert window.marker_label.isVisible(), "Spectrum label expired while the marker stayed still"
    main.resize(1380, 900)
    window.archive_splitter.moveSplitter(330, 1)
    app.processEvents()
    for fraction in (0.01, 0.99):
        hz = window.frequency_axis[0] + fraction * (window.frequency_axis[-1] - window.frequency_axis[0])
        position = window.plot.getViewBox().mapViewToScene(QtCore.QPointF(hz, -80))
        window.move_marker(position)
        assert window.plot.viewport().rect().contains(window.marker_label.geometry())
        marker_point = window.plot.mapFromScene(window.plot.getViewBox().mapViewToScene(QtCore.QPointF(
            window.frequency_axis[window.marker_index], window.current["power_dbm"][window.marker_index])))
        assert not window.marker_label.geometry().contains(marker_point), "Label covers the spectrum point"
    Path(".scratch").mkdir(exist_ok=True)
    window.plot.grab().save(".scratch/spectrum-marker-preview.png")
    app.sendEvent(window.plot.viewport(), QtCore.QEvent(QtCore.QEvent.Type.Leave))
    assert not window.marker_label.isVisible() and not window.cross_x.isVisible()
    target = ui.ROOT / "spectrum.csv"
    with patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=(str(target), "")):
        window.export_csv()
    assert len(target.read_text(encoding="utf-8-sig").splitlines()) == 802
    recovery = ui.ROOT / "data" / "rf-pending"
    assert not list(recovery.glob("*.json"))
    with patch("gsmbench.rsa_ui.SpectrumStore.save_many", side_effect=OSError("test write failure")):
        window.launch("single")
        wait_worker(window)
    assert "test write failure" in window.status.text()
    assert len(list(recovery.glob("*.json"))) == 1
    window.recover()
    assert not list(recovery.glob("*.json"))
    assert len(window.spectra) == 5
    # Every direction is acquired, calculated, and persisted without real hardware.
    from gsmbench.rf_methods import ISOLATION_PORTS
    window.use_smu.setChecked(True)
    window.source_path_known.setChecked(True)
    window.settling_s.setValue(0)
    for method, (source, destination) in ISOLATION_PORTS.items():
        window.method.setCurrentIndex(window.method.findData(method))
        window.launch("single")
        wait_worker(window)
        assert window.last_error is None, window.last_error
        assert window.current["result"]["status"] == "preliminary", window.current["result"]
        assert abs(window.current["result"]["value_db"] - (45 if source == "LO" else 55)) < 0.001
        enabled = [key for key, channel in window.current["generator"]["channels"].items() if channel["output"]]
        assert enabled == (["1"] if source == "LO" else ["2"])
    window.method.setCurrentIndex(window.method.findData("conversion"))
    # Manual SMU ON is explicit, closes through confirmed OFF, and locks its endpoint.
    window.launch("smu_on")
    wait_worker(window)
    assert window.generator_active and not window.demo.isEnabled()
    assert not window.smu_host.isEnabled()
    main.close()
    assert main.isVisible()
    wait_worker(window)
    assert not window.generator_active and window.demo.isEnabled()
    # Automated level sweep and a P1dB result with generator provenance.
    window.use_smu.setChecked(True)
    window.source_path_known.setChecked(True)
    window.settling_s.setValue(0)
    window.sweep_kind.setCurrentIndex(window.sweep_kind.findData("rf_power"))
    window.series_count.setValue(9)
    window.interval_s.setValue(0)
    window.launch("series")
    wait_worker(window)
    assert window.last_error is None, window.last_error
    assert window.saved_count == 9 and not window.generator_active
    assert window.current["series_summary"]["status"] == "preliminary"
    assert abs(window.current["series_summary"]["result"]["input_p1db_dbm"] + 10) < 0.001
    assert window.current["generator"]["channels"]["1"]["output"] == 1
    window.show_series_curve()
    assert window.showing_series and len(window.plot.listDataItems()[0].getData()[0]) == 9
    # Matrix template: every RF frequency × RF level × LO level, and CSV summary.
    window.sweep_kind.setCurrentIndex(window.sweep_kind.findData("matrix"))
    assert "350" in window.matrix_count.text()
    window.matrix_rf_mhz.setText("3.65;14.15")
    window.matrix_rf_dbm.setText("-40;-30;-20;-10;0")
    window.matrix_lo_dbm.setText("7;13")
    assert "20 спектров" in window.matrix_count.text()
    assert not window.series_count.isEnabled()
    window.launch("series")
    wait_worker(window)
    assert window.last_error is None, window.last_error
    assert window.saved_count == 20 and not window.generator_active
    assert window.current["series_summary"]["kind"] == "matrix_p1db"
    assert len(window.current["series_summary"]["slices"]) == 4
    window.show_series_curve()
    assert len(window.plot.listDataItems()) == 2
    axis = window.plot.getAxis("bottom")
    assert axis.autoSIPrefixScale == 1
    assert axis.tickStrings([-40, -20, 0], axis.autoSIPrefixScale * axis.scale, 20) == ["-40", "-20", "0"]
    app.processEvents()
    Path(".scratch").mkdir(exist_ok=True)
    window.grab().save(".scratch/mixer-matrix-preview.png")
    series_csv = ui.ROOT / "matrix.csv"
    with patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=(str(series_csv), "")):
        window.export_series_csv()
    assert len(series_csv.read_text(encoding="utf-8-sig").splitlines()) == 21
    # Excel-style archive filters drive charts and both exports.
    rf_column = window.archive_headers.index("RF, МГц")
    level_column = window.archive_headers.index("RF / IF, dBm")
    window.column_filters = {rf_column: {"3.650000", "14.150000"}, level_column: {"-30.000"}}
    window.filter_archive()
    assert window.table.horizontalHeader().active_filters == {rf_column, level_column}
    assert len(window.chart_spectra(False)) == 4
    before = {key: set(values) for key, values in window.column_filters.items()}
    with patch("gsmbench.ui.ColumnFilterDialog") as dialog:
        dialog.return_value.exec.return_value = QtWidgets.QDialog.DialogCode.Rejected
        window.show_archive_filter(level_column)
    assert window.column_filters == before
    window.show_charts()
    window.charts.x_axis.setCurrentIndex(window.charts.x_axis.findData("rf_hz"))
    window.charts.y_axis.setCurrentIndex(window.charts.y_axis.findData("raw_loss"))
    window.chart_spectra(False)[0]["averaging"]["marker_std_db"] = 0.025
    window.charts.build_chart()
    assert len(window.charts.groups) == 2, window.charts.status.text()
    assert any(point["sweep_std_db"] > 0 for group in window.charts.groups for point in group["points"])
    assert all(len(group["points"]) == 2 for group in window.charts.groups)
    chart_csv, chart_png = ui.ROOT / "chart.csv", ui.ROOT / "chart.png"
    with patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=(str(chart_csv), "")):
        window.charts.export_csv()
    assert len(chart_csv.read_text(encoding="utf-8-sig").splitlines()) == 5
    with patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=(str(chart_png), "")):
        window.charts.export_png()
    assert QtGui.QImage(str(chart_png)).width() == 2400, window.charts.status.text()
    main.resize(1380, 900)
    app.processEvents()
    main.grab().save(".scratch/archive-charts-preview.png")
    visible = [index for index in range(window.table.rowCount()) if not window.table.isRowHidden(index)]
    window.table.clearSelection()
    for index in visible[:2]:
        window.table.selectionModel().select(window.table.model().index(index, 0),
            QtCore.QItemSelectionModel.SelectionFlag.Select | QtCore.QItemSelectionModel.SelectionFlag.Rows)
    window.charts.scope.setCurrentIndex(1)
    window.charts.build_chart()
    assert sum(len(group["points"]) for group in window.charts.groups) == 2
    assert "●" in window.table.horizontalHeaderItem(level_column).text()
    window.clear_archive_filters()
    assert len(window.chart_spectra(False)) == len(window.spectra)
    assert not window.charts.groups, "A stale chart survived a change of filters"
    window.sweep_kind.setCurrentIndex(window.sweep_kind.findData("fixed"))
    # Stop a long series and refuse closing the parent until its worker exits.
    window.series_count.setValue(1000)
    window.interval_s.setValue(1)
    window.launch("series")
    QtTest.QTest.qWait(50)
    main.close()
    assert main.isVisible()
    wait_worker(window)
    window.series_count.setValue(3)
    unconfirmed = copy.deepcopy(window.spectra[0])
    unconfirmed["id"] = str(uuid.uuid4())
    unconfirmed["context"].update(pin_known=False, path_known=False)
    window.store().save_many([unconfirmed])
    window.reload()
    row = next(index for index, item in enumerate(window.spectra) if item["id"] == unconfirmed["id"])
    assert "уровень на входе" in window.table.item(row, 13).text()
    assert "ослабление тракта" in window.table.item(row, 11).toolTip()
    window.views.setCurrentIndex(0)
    assert window.archive_result_note.isVisible()
    app.processEvents()
    Path(".scratch").mkdir(exist_ok=True)
    window.grab().save(".scratch/rsa-ui-preview.png")
    main.grab().save(".scratch/main-workspaces-preview.png")
    saved = window.settings()
    main.close()
    restored = ui.Window()
    restored.show_rsa()
    assert restored.rsa_window.settings() == saved
    assert restored.rsa_window.worker is None
    restored.close()
print("RSA UI smoke passed: demo acquisition, dedicated DB, IF presets, archive, CSV, STOP, settings")
