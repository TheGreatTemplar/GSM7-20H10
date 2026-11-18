"""A large synthetic archive must not starve the GUI event loop."""

import copy
from pathlib import Path
import tempfile
import time
import uuid

from PySide6 import QtCore, QtWidgets
from gsmbench import ui
from gsmbench.rf_storage import SpectrumStore
from gsmbench.rsa import AnalyzerConfig, demo_spectrum


app = QtWidgets.QApplication([])
with tempfile.TemporaryDirectory() as directory:
    ui.ROOT = Path(directory)
    ui.SETTINGS = ui.ROOT / "settings.local.json"
    sample = demo_spectrum(AnalyzerConfig(points=101))
    sample["context"] = dict(method="spectrum", sample="LARGE", rf_hz=14e6, lo_hz=25059200, if_hz=11059200)
    rows = []
    for index in range(2001):
        item = copy.deepcopy(sample)
        item["id"] = str(uuid.uuid4())
        rows.append(item)
    SpectrumStore(ui.ROOT / "data" / "mixers-demo.db", True).save_many(rows)
    main = ui.Window()
    main.show()
    main.show_rsa()
    window = main.rsa_window
    ticks = []
    timer = QtCore.QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    window.demo.setChecked(True)
    window.reload()
    loop = QtCore.QEventLoop()
    poll = QtCore.QTimer()
    poll.setInterval(20)
    poll.timeout.connect(lambda: loop.quit() if not window.archive_loading and window.archive_worker is None else None)
    poll.start()
    QtCore.QTimer.singleShot(20000, loop.quit)
    loop.exec()
    assert not window.archive_loading and window.archive_worker is None
    assert len(window.spectra) == 2001
    assert len(ticks) >= 3
    assert max(b - a for a, b in zip(ticks, ticks[1:])) < 1, "GUI event loop starved during refresh"
    assert window.table.horizontalHeader().sectionResizeMode(0) == QtWidgets.QHeaderView.ResizeMode.Interactive
    main.close()
print("Archive responsiveness smoke passed")
