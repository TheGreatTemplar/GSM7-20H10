"""Render print figures from a full mixer run without contacting instruments."""

import json
import math
from pathlib import Path
import sys

from PySide6 import QtGui, QtWidgets
import pyqtgraph as pg
import pyqtgraph.exporters


COLORS = ["#245b9e", "#168477", "#c17a16", "#8b508c"]


def main():
    folder = Path(sys.argv[1]).resolve()
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (folder / "journal.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    estimates = json.loads((folder / "p1db.json").read_text(encoding="utf-8"))
    report = json.loads((folder / "report.json").read_text(encoding="utf-8")) if (folder / "report.json").exists() else {}
    output = folder / "report" / "figures"
    output.mkdir(parents=True, exist_ok=True)
    app = QtWidgets.QApplication([])
    QtGui.QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
    app.setFont(QtGui.QFont("Segoe UI", 11))
    pg.setConfigOptions(background="w", foreground="#182d41", antialias=True)

    def plot(title, xlabel, ylabel, name, curves, height=420):
        widget = pg.PlotWidget()
        widget.resize(1100, height)
        widget.setTitle(title, size="15pt", color="#182d41")
        widget.setLabel("bottom", xlabel, **{"font-size": "14pt"})
        widget.setLabel("left", ylabel, **{"font-size": "14pt"})
        for axis in ("bottom", "left"):
            widget.getAxis(axis).setSIPrefixEnableRanges(())
            widget.getAxis(axis).enableAutoSIPrefix(False)
            widget.getAxis(axis).setStyle(tickFont=QtGui.QFont("Segoe UI", 11))
        widget.getAxis("left").setWidth(95)
        widget.getAxis("bottom").setHeight(52)
        widget.showGrid(x=True, y=True, alpha=0.16)
        legend = widget.addLegend(offset=(12, 12), labelTextSize="18pt", brush="#ffffffde", pen=None)
        if name.endswith("-loss.png") or name in ("p1db-overview.png", "p1db-recheck.png"):
            legend.setColumnCount(len(curves))
            finite_values = [point[1] for _, points in curves for point in points if math.isfinite(point[1])]
            if finite_values:
                low, high = min(finite_values), max(finite_values)
                spread = max(0.05, high - low)
                widget.setYRange(low - spread * 0.05, high + spread * 0.3, padding=0)
        for index, (label, points) in enumerate(curves):
            if not points:
                continue
            points = sorted(points)
            widget.plot([p[0] for p in points], [p[1] for p in points],
                        pen=pg.mkPen(COLORS[index % len(COLORS)], width=2.5),
                        symbol="o", symbolSize=6, symbolBrush=COLORS[index % len(COLORS)], name=label,
                        connect="finite")
        widget.show()
        app.processEvents()
        app.processEvents()
        exporter = pg.exporters.ImageExporter(widget.plotItem)
        exporter.parameters()["width"] = 2000
        exporter.export(str(output / name))
        widget.close()

    plot("Потери тракта на центрах диапазонов при RF −30 dBm", "Частота RF, МГц", "Разность уровней, дБ",
         "overview.png", [(f"LO +{lo} dBm", [(r["rf_hz"] / 1e6, r["observed_loss_db"]) for r in rows
             if r["phase"] == "matrix" and r["position"] == "center" and r["rf_dbm"] == -30 and r["lo_dbm"] == lo])
             for lo in (10, 13, 16, 20)], height=410)
    for band, low, high in manifest["bands"]:
        selected = [r for r in rows if r["band_m"] == band]
        if not selected:
            continue
        center = (low + high) // 2
        plot(f"{band} м   Передаточная характеристика в центре диапазона", "RF генератора, dBm", "Уровень ПЧ на RSA, dBm",
             f"band-{band}-power.png", [(f"LO +{lo} dBm", [(r["rf_dbm"], r["output_dbm"]) for r in selected
                 if r["rf_hz"] == center and r["lo_dbm"] == lo]) for lo in (10, 13, 16, 20)])
        plot(f"{band} м   Зависимость потерь от уровня гетеродина при RF −30 dBm", "LO генератора, dBm", "Разность уровней, дБ",
             f"band-{band}-loss.png", [(label, [(r["lo_dbm"], r["observed_loss_db"]) for r in selected
                 if r["phase"] == "matrix" and r["position"] == position and r["rf_dbm"] == -30])
                 for position, label in (("low", "Нижний край"), ("center", "Центр"), ("high", "Верхний край"))])
    plot("Компрессия 1 дБ на центрах диапазонов", "Частота RF, МГц", "Входной P1dB генератора, dBm",
         "p1db-overview.png", [(f"LO +{lo} dBm", [(e["rf_hz"] / 1e6, e.get("input_generator_p1db_dbm", float("nan"))) for e in estimates
             if e["position"] == "center" and e["lo_dbm"] == lo])
             for lo in (10, 13, 16, 20)], height=410)
    if report.get("rechecks"):
        latest = report["rechecks"][-1]
        values = json.loads((Path(latest["folder"]) / "means.json").read_text(encoding="utf-8"))
        gain = latest["estimate"]["small_signal_gain_db"]
        points = [(row["rf_dbm"], gain - row["output_dbm"] + row["rf_dbm"]) for row in values]
        low, high = min(x for x, _ in points), max(x for x, _ in points)
        plot("Перепроверка P1dB   RF 51 МГц   LO +20 dBm", "RF генератора, dBm", "Компрессия, дБ",
             "p1db-recheck.png", [("Измерено", points), ("Порог 1 дБ", [(low, 1), (high, 1)])], height=390)
    print(f"Figures: {output}")


if __name__ == "__main__":
    main()
