"""Render real Qt layouts on configured Full HD/4K virtual screens, without I/O."""

import argparse
import json
import os
from pathlib import Path
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument("--width", type=int, default=1920)
parser.add_argument("--height", type=int, default=1080)
parser.add_argument("--scale", type=float, default=1)
options = parser.parse_args()
output = Path(".scratch")
output.mkdir(exist_ok=True)
name = f"{options.width}x{options.height}-{round(options.scale * 100)}"
config = output / ("screen-" + name + ".json")
config.write_text(json.dumps(dict(screens=[dict(name="Layout test", x=0, y=0, width=options.width,
    height=options.height - round(40 * options.scale), logicalDpi=96, logicalBaseDpi=96, dpr=1)])), encoding="utf-8")
os.environ["QT_QPA_PLATFORM"] = "offscreen:configfile=" + config.as_posix()
os.environ["QT_SCALE_FACTOR"] = str(options.scale)

from PySide6 import QtCore, QtGui, QtWidgets
from gsmbench import ui
from gsmbench.rf_labels import INPUT_ATTENUATION_A, INPUT_ATTENUATION_B

app = QtWidgets.QApplication([])
app.setStyle("Fusion")
QtGui.QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
app.setFont(QtGui.QFont("Segoe UI", 10))


def dimensions(style):
    parent = QtWidgets.QWidget()
    parent.setStyleSheet(style)
    button = QtWidgets.QPushButton("Измерить", parent)
    tabs = QtWidgets.QTabBar(parent)
    tabs.addTab("Первый тон")
    parent.ensurePolished()
    button.ensurePolished()
    tabs.ensurePolished()
    return button.sizeHint().height(), tabs.sizeHint().height()


after = dimensions(ui.STYLE)

with tempfile.TemporaryDirectory() as directory:
    ui.ROOT = Path(directory)
    ui.SETTINGS = ui.ROOT / "settings.local.json"
    main = ui.Window()
    main.show()
    main.show_rsa()
    app.processEvents()
    available = app.primaryScreen().availableGeometry()
    main.setGeometry(available.adjusted(4, 4, -4, -34))
    app.processEvents()
    bench = main.rsa_window
    panel = bench.two_tone
    bench.views.setCurrentWidget(panel)
    panel.pages.setCurrentIndex(1)
    panel.calibration.method.setCurrentIndex(panel.calibration.method.findData("nrp"))
    app.processEvents()
    cal = panel.calibration
    assert main.frameGeometry().width() <= available.width(), (main.frameGeometry(), available)
    assert main.frameGeometry().height() <= available.height(), (main.frameGeometry(), available)
    assert cal.start_button.isVisible() and cal.plot.isVisible()
    assert main.rect().contains(cal.start_button.mapTo(main, cal.start_button.rect().bottomRight()))
    expected = QtWidgets.QBoxLayout.Direction.LeftToRight if cal.settings_scroll.viewport().width() >= 720 else QtWidgets.QBoxLayout.Direction.TopToBottom
    assert cal.settings_columns.direction() == expected
    for key, caption in (("loss1", INPUT_ATTENUATION_A), ("loss2", INPUT_ATTENUATION_B)):
        assert panel.form.labelForField(panel.fields[key]).text() == caption
        assert cal.form.labelForField(cal.fields[key]).text() == caption
    cal.fields["loss1"].setValue(16)
    assert panel.fields["loss1"].value() == 16
    panel.fields["loss2"].setValue(17)
    assert cal.fields["loss2"].value() == 17
    main.grab().save(str(output / ("layout-calibration-" + name + ".png")))
    panel.pages.setCurrentIndex(0)
    app.processEvents()
    for button in panel.pages.currentWidget().findChildren(QtWidgets.QPushButton):
        if button.isVisible():
            assert main.rect().contains(button.mapTo(main, button.rect().bottomRight())), button.text()
    main.grab().save(str(output / ("layout-two-tone-" + name + ".png")))
    panel.pages.setCurrentIndex(1)
    for index, label in ((1, "generator"), (2, "analyzer")):
        bench.setup_tabs.setCurrentIndex(index)
        app.processEvents()
        assert bench.form_panel.width() <= bench.setup_panel_widths[str(index)] + 2, (
            label, bench.form_panel.width(), bench.setup_panel_widths)
        assert bench.form_panel.width() < 400, (label, bench.form_panel.width())
        scroll = bench.setup_tabs.currentWidget()
        assert scroll.horizontalScrollBar().maximum() == 0, label
        for button in bench.setup_tabs.currentWidget().findChildren(QtWidgets.QPushButton):
            form = button.parentWidget().layout()
            if isinstance(form, QtWidgets.QFormLayout):
                assert form.getWidgetPosition(button)[1] == QtWidgets.QFormLayout.ItemRole.SpanningRole
        main.grab().save(str(output / ("layout-" + label + "-" + name + ".png")))
    bench.setup_tabs.setCurrentIndex(1)
    app.processEvents()
    old_width = bench.form_panel.width()
    bench.panels_splitter.moveSplitter(max(300, old_width - 20), 1)
    changed_width = bench.form_panel.width()
    if old_width > 300:
        assert changed_width < old_width
    bench.setup_tabs.setCurrentIndex(2)
    bench.setup_tabs.setCurrentIndex(1)
    app.processEvents()
    assert bench.form_panel.width() == changed_width
    main.close()
print(json.dumps(dict(screen=name, logical=[available.width(), available.height()],
    button_height=after[0], tab_height=after[1],
    columns="two" if expected == QtWidgets.QBoxLayout.Direction.LeftToRight else "one")))
