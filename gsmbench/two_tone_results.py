"""Tables, point annotations and plots for live and archived two-tone series."""

import math

from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

from .plot_hover import PointHover
from .table_filters import configure_table


def intercept(side):
    return side.get("ip3_estimate") or side.get("ip3") or {}


def columns():
    return ["RF1, МГц", "RF2, МГц", "Pin A, dBm", "Pin B, dBm", "SMU A, dBm", "SMU B, dBm",
        "Pout low, dBm", "Pout high, dBm", "G low, дБ", "G high, дБ", "Pср, Вт", "PEP, Вт",
        "IM3 low, dBc", "IM3 high, dBc", "IIP3 low, dBm", "IIP3 high, dBm",
        "OIP3 low, dBm", "OIP3 high, dBm", "U IM3, дБ (k=2)"] + [f"H{order}, dBc" for order in range(2, 8)] + ["Основа IP3", "UUID", "LO, МГц", "LO, dBm"]


def row_values(point):
    plan, result = point["plan"], point["result"]
    sides = result["sides"]
    pins = [plan["level1_dbm"] - plan["loss1_db"], plan["level2_dbm"] - plan["loss2_db"]]
    reverse = plan["mode"] == "mixer" and abs(plan["lo_hz"] - plan["f1_hz"]) > abs(plan["lo_hz"] - plan["f1_hz"] - plan["spacing_hz"])
    output_pins = list(reversed(pins)) if reverse else pins
    values = [plan["f1_hz"] / 1e6, (plan["f1_hz"] + plan["spacing_hz"]) / 1e6,
        *pins, plan["level1_dbm"], plan["level2_dbm"], result["fundamental_low_dbm"], result["fundamental_high_dbm"],
        result["fundamental_low_dbm"] - output_pins[0], result["fundamental_high_dbm"] - output_pins[1],
        result["output_average_w"], result["output_pep_w"]]
    for side in ("low", "high"):
        item = sides[side]
        values.append(item["im3_dbc"] if item["resolved"] else f"<{item['noise_limit_dbc']:.2f}")
    for metric in ("iip3_dbm", "oip3_dbm"):
        values.extend(intercept(sides[side]).get(metric) for side in ("low", "high"))
    values.append(result.get("uncertainty", {}).get("expanded_uncertainty_k2_db") if result.get("value_db") is not None else None)
    for order in range(2, 8):
        band = next((item for item in point.get("harmonics", []) if item["order"] == order), None)
        values.append(None if band is None else band["dbc"] if band["resolved"] else f"<{band['noise_limit_dbc']:.2f}")
    values.extend(["Подтверждённые поправки; оценка IP3" if result["calibrated"] else "Номинальные ослабления; оценка IP3", point["spectrum_id"]])
    values.extend([plan["lo_hz"] / 1e6 if plan["mode"] == "mixer" else None, plan["lo_dbm"] if plan["mode"] == "mixer" else None])
    return values


class TwoToneResults(QtWidgets.QWidget):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.rows = []
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        tools = QtWidgets.QHBoxLayout()
        self.metric_selector = QtWidgets.QComboBox()
        for title, value in (("IM3 и гармоники", "distortion"), ("OIP3", "oip3_dbm"), ("IIP3", "iip3_dbm"),
                ("Усиление", "gain"), ("Мощность и PEP", "power")):
            self.metric_selector.addItem(title, value)
        self.x_axis = QtWidgets.QComboBox()
        for title, value in (("X: вход одного тона, dBm", "pin"), ("X: выходная PEP, мВт", "pep"), ("X: частота RF1, МГц", "frequency")):
            self.x_axis.addItem(title, value)
        self.frequency = QtWidgets.QComboBox()
        self.frequency.addItem("Все частоты", None)
        for widget in (self.metric_selector, self.x_axis, self.frequency):
            widget.setMaximumWidth(250)
            tools.addWidget(widget)
            widget.currentIndexChanged.connect(self.redraw)
        tools.addStretch()
        layout.addLayout(tools)
        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        layout.addWidget(self.splitter, 1)
        self.plot = pg.PlotWidget(background="#111923")
        self.plot.setMenuEnabled(False)
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.addLegend()
        self.plot.plotItem.legend.setOffset((-10, 10))
        self.plot.plotItem.legend.setBrush(pg.mkBrush(17, 25, 35, 220))
        self.hover = PointHover(self.plot, self.point_hint)
        self.splitter.addWidget(self.plot)
        self.table = QtWidgets.QTableWidget(0, len(columns()))
        configure_table(self.table)
        self.table.setHorizontalHeaderLabels(columns())
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setDefaultSectionSize(135)
        self.table.setMinimumHeight(100)
        self.splitter.addWidget(self.table)
        self.splitter.setSizes([420, 180])

    def settings(self):
        return dict(metric=self.metric_selector.currentData(), x=self.x_axis.currentData(), splitter=self.splitter.sizes())

    def restore(self, settings):
        for widget, key in ((self.metric_selector, "metric"), (self.x_axis, "x")):
            index = widget.findData(settings.get(key))
            if index >= 0:
                widget.setCurrentIndex(index)
        sizes = settings.get("splitter")
        if isinstance(sizes, list) and len(sizes) == 2 and all(isinstance(value, int) and value > 0 for value in sizes):
            self.splitter.setSizes(sizes)

    def update_points(self):
        selected = self.frequency.currentData()
        blocker = QtCore.QSignalBlocker(self.frequency)
        self.frequency.clear()
        self.frequency.addItem("Все частоты", None)
        for hz in sorted({point["plan"]["f1_hz"] for point in self.owner.points}):
            self.frequency.addItem(f"{hz / 1e6:g} МГц", hz)
        self.frequency.setCurrentIndex(max(0, self.frequency.findData(selected)))
        del blocker
        self.redraw()

    def show_metric(self, metric):
        index = self.metric_selector.findData(metric)
        self.metric_selector.setCurrentIndex(index)
        self.update_points()

    def redraw(self, *args):
        selected = self.frequency.currentData()
        self.rows = [point for point in self.owner.points if selected is None or point["plan"]["f1_hz"] == selected]
        self.table.setRowCount(len(self.rows))
        for row, point in enumerate(self.rows):
            values = row_values(point)
            for column, value in enumerate(values):
                text = "—" if value is None else f"{value:.6g}" if isinstance(value, (int, float)) else str(value)
                item = QtWidgets.QTableWidgetItem(text)
                item.setToolTip(self.point_hint(dict(point=point)))
                if 14 <= column <= 17 and not point["result"]["calibrated"]:
                    item.setForeground(QtGui.QColor("#e6bf78"))
                self.table.setItem(row, column, item)
        self.hover.clear()
        self.plot.clear()
        self.hover.restore_lines()
        legend = self.plot.plotItem.legend
        legend.clear()
        legend.show()
        metric, axis = self.metric_selector.currentData(), self.x_axis.currentData()
        groups = {}
        for point in self.rows:
            plan = point["plan"]
            key = ((plan["level1_dbm"] - plan["loss1_db"]) if axis == "frequency" else plan["f1_hz"],
                plan["lo_dbm"] if plan["mode"] == "mixer" else None)
            groups.setdefault(key, []).append(point)
        for index, ((group, lo), points) in enumerate(sorted(groups.items())):
            points.sort(key=lambda point: self.x_value(point, axis))
            prefix = f"Pin {group:g} dBm" if axis == "frequency" else f"{group / 1e6:g} МГц"
            if lo is not None:
                prefix += f" · LO {lo:g} dBm"
            color = ("#64dcd4", "#f4bd61", "#a9b5ff", "#e895bd", "#90cc9a", "#79b9e5")[index % 6]
            curves = []
            for side in ("low", "high"):
                if metric in ("oip3_dbm", "iip3_dbm"):
                    values = [intercept(point["result"]["sides"][side]).get(metric) for point in points]
                    label = ("OIP3" if metric == "oip3_dbm" else "IIP3") + " · оценка · " + side
                elif metric == "gain":
                    values = [row_values(point)[8 if side == "low" else 9] for point in points]
                    label = "G " + side
                elif metric == "distortion":
                    values = [point["result"]["sides"][side]["im3_dbc"] for point in points]
                    label = "IM3 " + side
                else:
                    values = [point["result"]["output_average_w" if side == "low" else "output_pep_w"] * 1000 for point in points]
                    label = "Pср" if side == "low" else "PEP по основным тонам"
                curves.append((label, values, side == "high"))
            if metric == "distortion":
                for order in range(2, 8):
                    values = [next((band.get("dbc") for band in point.get("harmonics", []) if band["order"] == order), None) for point in points]
                    if any(value is not None for value in values):
                        curves.append((f"H{order}", values, True))
            for label, values, dashed in curves:
                valid = [(point, value) for point, value in zip(points, values) if value is not None and math.isfinite(value)]
                if not valid:
                    continue
                # Keep gaps at unresolved points instead of joining across them.
                x = [self.x_value(point, axis) for point in points]
                y = [float('nan') if value is None else value for value in values]
                curve = self.plot.plot(x, y, connect="finite", pen=pg.mkPen(color, width=2,
                    style=QtCore.Qt.PenStyle.DashLine if dashed else QtCore.Qt.PenStyle.SolidLine), symbol="o", name=prefix + " · " + label)
                self.hover.attach(curve, [self.x_value(point, axis) for point, _ in valid], [value for _, value in valid],
                    [dict(point=point, curve=label, channel=int(dashed)) for point, _ in valid])
        self.plot.setLabel("bottom", {"pin": "Вход одного тона, dBm", "pep": "Выходная PEP по основным тонам, мВт", "frequency": "Частота RF1, МГц"}[axis])
        self.plot.setLabel("left", {"distortion": "Относительно одного основного тона, dBc", "power": "Мощность, мВт",
            "gain": "Усиление, дБ", "oip3_dbm": "OIP3, dBm · оценка", "iip3_dbm": "IIP3, dBm · оценка"}[metric])
        for name in ("bottom", "left"):
            self.plot.getAxis(name).enableAutoSIPrefix(False)
        if len(legend.items) > 10:
            legend.hide()
        self.plot.enableAutoRange()

    @staticmethod
    def x_value(point, axis):
        return (point["plan"]["f1_hz"] / 1e6 if axis == "frequency" else
            point["result"]["output_pep_w"] * 1000 if axis == "pep" else
            point["plan"]["level1_dbm"] - point["plan"]["loss1_db"])

    @staticmethod
    def point_hint(data):
        point = data["point"]
        values = row_values(point)
        keys = [0, 1, 2, 3, 6, 7, 8, 9, 12, 13, 14, 15, 16, 17]
        text = data.get("curve", "Двухтоновая точка")
        for key in keys:
            value = values[key]
            text += "\n" + columns()[key] + ": " + ("—" if value is None else f"{value:.3f}" if isinstance(value, (int, float)) else str(value))
        return text + "\n" + values[25] + "\nIP3 — экстраполяция; не выходная мощность. В компрессии оценка недостоверна."
