"""Offscreen end-to-end GUI check using only a temporary demo database."""

from pathlib import Path
import tempfile
import time

from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from unittest.mock import patch

from gsmbench import ui
from gsmbench import __release_date__, __version__
from tests.test_core import make_run


app = QtWidgets.QApplication([])
font_path = Path("C:/Windows/Fonts/segoeui.ttf")
if font_path.exists():
    QtGui.QFontDatabase.addApplicationFont(str(font_path))
    app.setFont(QtGui.QFont("Segoe UI", 10))
with tempfile.TemporaryDirectory() as directory:
    ui.ROOT = Path(directory)
    ui.SETTINGS = ui.ROOT / "settings.local.json"
    window = ui.Window()
    dialog = ui.ColumnFilterDialog("Частота", ["1.850000", "3.650000", "14.150000"], {"3.650000"}, window)
    dialog.show()
    dialog.search.setText("3.65")
    dialog.clear_all.click()
    assert dialog.selected_values() == set()
    dialog.select_all.click()
    assert len(dialog.selected_values()) == 3, "Select all ignored hidden search results"
    dialog.clear_all.click()
    dialog.check_visible(True)
    assert dialog.selected_values() == {"3.650000"}
    assert dialog.select_all.checkState() == QtCore.Qt.CheckState.PartiallyChecked
    app.processEvents()
    Path(".scratch").mkdir(exist_ok=True)
    dialog.grab().save(".scratch/filter-dialog-preview.png")
    dialog.close()
    errors = []
    window.error = errors.append
    window.show()
    app.processEvents()
    assert window.resource.text() == "TCPIP0::172.16.236.32::5025::SOCKET"
    available = window.screen().availableGeometry()
    assert abs(window.frameGeometry().center().x() - available.center().x()) <= 2
    assert abs(window.frameGeometry().center().y() - available.center().y()) <= 2
    assert window.model.text() == "КД419"
    with patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=(str(ui.ROOT / "new.db"), "")):
        window.create_db()
    assert (ui.ROOT / "new.db").exists()
    assert not window.windowIcon().isNull()
    assert not window.windowIcon().pixmap(32, 32).isNull()
    window.help_action.trigger()
    app.processEvents()
    assert window.help_dialog.isVisible()
    from gsmbench.help_ui import HELP_PAGES
    assert window.help_dialog.tabs.count() == len(HELP_PAGES)
    mixer_index = next(index for index in range(window.help_dialog.tabs.count())
        if window.help_dialog.tabs.tabText(index) == "Смеситель")
    mixer_help = window.help_dialog.tabs.widget(mixer_index)
    mixer_image = mixer_help.document().resource(QtGui.QTextDocument.ResourceType.ImageResource,
                                               QtCore.QUrl("gsmbench:mixer"))
    assert isinstance(mixer_image, QtGui.QImage) and not mixer_image.isNull()
    assert "VD017" in mixer_help.toPlainText()
    window.help_dialog.tabs.setCurrentIndex(mixer_index)
    app.processEvents()
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
    assert window.progress.value() == window.progress.maximum() == 20
    assert window.progress.text() == "20 / 20 точек"
    window.table.selectRow(0)
    app.processEvents()
    curve = window.plot.listDataItems()[0]
    window.plot.autoRange()
    QtTest.QTest.qWait(100)
    x_hover, y_hover = curve.getData()
    scene_position = window.plot.getViewBox().mapViewToScene(QtCore.QPointF(x_hover[15], y_hover[15]))
    QtTest.QTest.mouseMove(window.plot.viewport(), window.plot.mapFromScene(scene_position))
    QtTest.QTest.qWait(100)
    assert window.hover_selection is not None, "Mouse hover did not select measurement"
    assert window.hover_label.isVisible()
    QtTest.QTest.qWait(11000)
    assert window.hover_label.isVisible(), "Stationary hover expired"
    assert all(line.isVisible() for line in window.hover_lines)
    QtTest.QTest.mouseMove(window.plot.viewport(), QtCore.QPoint(2, 2))
    QtTest.QTest.qWait(100)
    assert not window.hover_label.isVisible(), "Hover remained after leaving point"
    window.hover_points(curve, [curve.scatter.points()[5]])
    assert window.hover_selection[1].current == window.runs[0]["points"][5]["current"]
    assert all(line.isVisible() for line in window.hover_lines)
    line = window.hover_lines[0].line()
    assert line.x1() == line.x2() == window.runs[0]["points"][5]["voltage"]
    window.hover_points(curve, [])
    assert not any(line.isVisible() for line in window.hover_lines)
    assert not window.hover_label.isVisible()
    x, y = curve.getData()
    original_x, original_y = x.copy(), y.copy()
    window.swap_axes_button.click()
    x, y = curve.getData()
    assert (x == original_y).all() and (y == original_x).all()
    assert window.plot.getAxis("bottom").labelUnits == "A"
    window.hover_points(curve, [curve.scatter.points()[5]])
    assert window.hover_lines[0].line().x1() == window.runs[0]["points"][5]["current"]
    window.hide_hover()
    window.plot_selected()
    x, y = window.plot.listDataItems()[0].getData()
    assert (x == original_y).all() and (y == original_x).all()
    window.swap_axes_button.click()
    x, y = window.plot.listDataItems()[0].getData()
    assert (x == original_x).all() and (y == original_y).all()
    assert window.plot.getAxis("bottom").labelUnits == "V"
    window.plot.setXRange(10, 11, padding=0)
    window.plot.setYRange(10, 11, padding=0)
    window.reset_scale_button.click()
    app.processEvents()
    x_range, y_range = window.plot.getViewBox().viewRange()
    assert x_range[0] <= min(original_x) <= max(original_x) <= x_range[1]
    assert y_range[0] <= min(original_y) <= max(original_y) <= y_range[1]
    assert all(window.plot.getViewBox().autoRangeEnabled())
    # Hover must leave the point and its marker visible at every viewport edge.
    curve = window.plot.listDataItems()[0]
    point = curve.scatter.points()[10]
    px, py = point.pos().x(), point.pos().y()
    for fraction_x in (0.02, 0.5, 0.98):
        for fraction_y in (0.02, 0.5, 0.98):
            window.plot.setXRange(px - fraction_x, px + 1 - fraction_x, padding=0)
            window.plot.setYRange(py - fraction_y, py + 1 - fraction_y, padding=0)
            app.processEvents()
            window.hover_points(curve, [point])
            position = window.plot.mapFromScene(window.plot.getViewBox().mapViewToScene(point.pos()))
            marker_rect = QtCore.QRect(position - QtCore.QPoint(7, 7), QtCore.QSize(15, 15))
            assert not window.hover_label.geometry().intersects(marker_rect), (fraction_x, fraction_y)
            assert window.plot.viewport().rect().contains(window.hover_label.geometry())
    window.reset_plot_scale()
    Path(".scratch").mkdir(exist_ok=True)
    import shutil
    shutil.copyfile(ui.ROOT / "help-preview.png", ".scratch/help-preview.png")
    shutil.copyfile(ui.ROOT / "about-preview.png", ".scratch/about-preview.png")
    window.hover_points(window.plot.listDataItems()[0], [window.plot.listDataItems()[0].scatter.points()[15]])
    app.processEvents()
    window.grab().save(".scratch/ui-preview.png")
    window.hide_hover()
    # Exercise real column dialogs and combine filters on a temporary database.
    fixtures = [make_run("FILTER-A"), make_run("FILTER-B"), make_run("FILTER-C")]
    fixtures[0].update(model="КД419", batch="Первая")
    fixtures[1].update(model="КД419", batch="Вторая", state="cancelled")
    fixtures[2].update(model="J310", batch="Первая")
    window.store().save_many(fixtures)
    window.refresh()

    def choose_column(column, values, accept=True):
        def finish_dialog():
            dialog = app.activeModalWidget()
            assert isinstance(dialog, ui.ColumnFilterDialog)
            for index in range(dialog.values.count()):
                item = dialog.values.item(index)
                item.setCheckState(QtCore.Qt.CheckState.Checked if item.data(QtCore.Qt.ItemDataRole.UserRole) in values
                                   else QtCore.Qt.CheckState.Unchecked)
            dialog.accept() if accept else dialog.reject()
        QtCore.QTimer.singleShot(0, finish_dialog)
        window.show_column_filter(column)

    choose_column(2, {"Первая", "Вторая"})
    assert window.filter_count.text() == "Показано: 3 / 4"
    choose_column(1, {"КД419"})
    assert window.table.horizontalHeader().active_filters == {1, 2}
    assert window.filter_count.text() == "Показано: 2 / 4"
    window.table.selectAll()
    assert {run["sample"] for run in window.selected()} == {"FILTER-A", "FILTER-B"}
    choose_column(5, {"complete"})
    assert [run["sample"] for run in window.selected()] == ["FILTER-A"]
    window.refresh()
    assert window.filter_count.text() == "Показано: 1 / 4"
    assert [run["sample"] for run in window.selected()] == ["FILTER-A"]
    window.table.selectAll()
    export_path = ui.ROOT / "filtered.csv"
    with patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=(str(export_path), "")):
        window.export_csv()
    import csv
    with export_path.open(encoding="utf-8-sig", newline="") as stream:
        assert {row["sample"] for row in csv.DictReader(stream, delimiter=";")} == {"FILTER-A"}
    choose_column(1, {"J310"}, accept=False)
    assert window.column_filters[1] == {"КД419"}
    window.table_search.setText("filter-b")
    assert window.filter_count.text() == "Показано: 0 / 4"
    assert not window.selected()
    window.clear_filters()
    assert window.filter_count.text() == "Показано: 4 / 4"
    dialog = ui.ColumnFilterDialog("Марка", ["КД419", "J310"], None, window)
    dialog.search.setText("кд")
    dialog.check_visible(False)
    assert dialog.selected_values() == {"J310"}
    dialog.deleteLater()
    choose_column(2, {"Первая", "Вторая"})
    window.table_search.setText("FILTER")
    window.grab().save(".scratch/filters-preview.png")
    window.range_stop.setText("100")
    window.generate_points()
    assert float(window.levels.text().split(";")[-1]) == 100
    window.wire_mode.setCurrentIndex(1)
    window.terminals.setCurrentIndex(1)
    window.delay.setValue(1)
    window.nplc.setValue(10)
    window.digits.setCurrentIndex(3)
    window.match_method.setCurrentIndex(1)
    assert not window.group_size.isEnabled() and window.group_size.currentText() == "4"
    balanced = [make_run("D1"), make_run("D2", 0.08), make_run("D3", 0.03), make_run("D4", 0.05)]
    with patch.object(window, "selected", return_value=balanced):
        window.match()
    assert "(D1 + D2) ↔ (D3 + D4)" in window.matches.toPlainText()
    assert "ΔΣmax" in window.matches.toPlainText()
    for position, sample in (("D1", "D1"), ("D2", "D2"), ("D3", "D3"), ("D4", "D4")):
        assert f"{position} схемы ← образец «{sample}»" in window.matches.toPlainText()
    assert not errors, errors
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
