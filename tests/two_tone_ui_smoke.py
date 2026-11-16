"""Two-tone workflow uses synthetic spectra and temporary databases only."""

from pathlib import Path
import tempfile
import time
from unittest.mock import patch

from PySide6 import QtGui, QtTest, QtWidgets

from gsmbench import ui


app = QtWidgets.QApplication([])
font = Path("C:/Windows/Fonts/segoeui.ttf")
if font.exists():
    QtGui.QFontDatabase.addApplicationFont(str(font))
    app.setFont(QtGui.QFont("Segoe UI", 10))


def wait(panel):
    deadline = time.monotonic() + 20
    while panel.worker is not None and time.monotonic() < deadline:
        QtTest.QTest.qWait(10)
    assert panel.worker is None, "Two-tone worker did not finish"


with tempfile.TemporaryDirectory() as directory:
    ui.ROOT = Path(directory)
    ui.SETTINGS = ui.ROOT / "settings.local.json"
    main = ui.Window()
    main.resize(1600, 1000)
    main.show()
    main.resize(1600, 1000)
    main.show_rsa()
    bench = main.rsa_window
    panel = bench.two_tone
    bench.views.setCurrentWidget(panel)
    bench.demo.setChecked(True)
    panel.analyzer_preset()
    assert "SMU A/1 и B/2" in panel.wiring.text()
    assert not panel.fields["lo_dbm"].isEnabled()
    panel.mode.setCurrentIndex(panel.mode.findData("mixer"))
    panel.fields["lo_dbm"].setValue(17)
    assert panel.plans()[0].lo_dbm == 17
    assert "АКИП → LO" in panel.wiring.text()
    assert panel.fields["lo_dbm"].isEnabled()
    panel.mode.setCurrentIndex(panel.mode.findData("pa"))
    panel.fields["calibrated"].setChecked(True)
    legacy = panel.settings()
    legacy.pop("source_layout")
    panel.restore(legacy)
    assert not panel.fields["calibrated"].isChecked()
    panel.fields["harmonics"].setValue(3)
    panel.fields["sweep"].setChecked(True)
    panel.fields["drive_stop"].setValue(-18)
    with patch("gsmbench.two_tone_ui.Smu200a", side_effect=AssertionError("Demo connected to SMU")), \
            patch("gsmbench.two_tone_ui.Ssg3000x", side_effect=AssertionError("Demo connected to AKIP")), \
            patch("gsmbench.two_tone_ui.Rsa6265", side_effect=AssertionError("Demo connected to RSA")):
        panel.start()
        wait(panel)
    assert len(panel.points) == 3, panel.status.text()
    assert all(len(point["harmonics"]) == 2 for point in panel.points)
    assert len(bench.spectra) == 9
    assert not panel.unsafe_outputs and not bench.generator_active
    assert not (ui.ROOT / "data" / "mixers.db").exists()
    panel.plot_distortion()
    assert len(panel.plot.listDataItems()) == 4
    app.processEvents()
    Path(".scratch").mkdir(exist_ok=True)
    main.grab().save(".scratch/two-tone-preview.png")
    panel.points = []
    panel.load_archive()
    assert len(panel.points) == 3, panel.status.text()
    assert len(panel.plot.listDataItems()) == 4
    for name, callback in (("two-tone.csv", panel.export_csv), ("two-tone.png", panel.export_png)):
        path = ui.ROOT / name
        with patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=(str(path), "")):
            callback()
        assert path.stat().st_size > 100
    assert QtGui.QImage(str(ui.ROOT / "two-tone.png")).width() == 2400
    assert len((ui.ROOT / "two-tone.csv").read_text(encoding="utf-8-sig").splitlines()) == 4
    panel.fields["wiring"].setChecked(True)
    saved = bench.settings()
    assert "wiring" not in saved["two_tone"]
    assert saved["two_tone"]["source_layout"] == "smu_ab_akip_lo"
    bench.demo.setChecked(False)
    panel.fields["wiring"].setChecked(False)
    panel.start()
    assert panel.worker is None and "подключите" in panel.status.text()
    bench.demo.setChecked(True)
    panel.fields["drive_stop"].setValue(-10)
    panel.start()
    panel.stop()
    wait(panel)
    assert not panel.unsafe_outputs
    app.processEvents()
    main.close()
    app.processEvents()
print("Two-tone UI smoke passed")
