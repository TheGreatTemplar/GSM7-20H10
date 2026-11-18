"""Offline charts from archived spectra, with conditions kept in separate curves."""

import csv
import json
import math
import statistics

from PySide6 import QtCore, QtWidgets
import pyqtgraph as pg
import pyqtgraph.exporters

from .rf_methods import ISOLATION_PORTS, METHOD_CHOICES, injection_port, target_frequency
from .rsa import mixer_result, peak_near
from .plot_hover import PointHover


X_AXES = [("RF, МГц", "rf_hz"), ("RF / IF генератора, dBm", "rf_level"), ("LO генератора, dBm", "lo_level")]
Y_AXES = [("Разность генератор → RSA, дБ", "raw_loss"),
          ("Потери / развязка с поправками, дБ", "corrected"), ("Уровень тона на RSA, dBm", "output")]


def source_level(spectrum, port):
    point = spectrum["context"]
    key = "lo_actual_dbm" if port == "LO" else "rf_actual_dbm"
    if key in point:
        return float(point[key])
    role = "lo_channel" if port == "LO" else "rf_channel"
    return float(spectrum["generator"]["channels"][str(point[role])]["level_dbm"])


def chart_groups(spectra, x_axis, y_axis):
    if x_axis not in dict((key, label) for label, key in X_AXES) or y_axis not in dict((key, label) for label, key in Y_AXES):
        raise ValueError("Неизвестная ось графика")
    groups, skipped = {}, 0
    for spectrum in spectra:
        try:
            point, settings = spectrum["context"], spectrum["settings"]
            method = point.get("method", "spectrum")
            if method in ("two_tone_pa", "two_tone_mixer", "harmonic_pa"):
                raise ValueError("Двухтоновые графики строятся на вкладке «Два тона»")
            try:
                rf_level = source_level(spectrum, "RF")
            except (KeyError, TypeError):
                rf_level = None
            try:
                lo_level = source_level(spectrum, "LO")
            except (KeyError, TypeError):
                lo_level = None
            x = point["rf_hz"] / 1e6 if x_axis == "rf_hz" else rf_level if x_axis == "rf_level" else lo_level
            if spectrum.get("if_overload"):
                raise ValueError("Перегрузка")
            if any(settings.get(key, 0) for key in ("amplitude_correction", "reference_offset_db", "external_gain_db")):
                raise ValueError("Амплитудные поправки RSA")
            if y_axis == "corrected":
                if method != "conversion" and method not in ISOLATION_PORTS:
                    raise ValueError("Этот вид результата не является потерями или развязкой")
                result = mixer_result(spectrum)
                if result["status"] != "preliminary":
                    raise ValueError("Нет результата с подтверждёнными поправками")
                y = result["value_db"]
            else:
                diagnostics = spectrum.get("diagnostics", {})
                output = diagnostics.get("output_dbm")
                if output is None:
                    output = (max(spectrum["power_dbm"]) if method == "spectrum" else
                              peak_near(spectrum, target_frequency(point), point["marker_window_hz"])["power_dbm"])
                y = output if y_axis == "output" else source_level(spectrum, injection_port(method)) - output
            if not math.isfinite(x) or not math.isfinite(y):
                raise ValueError("Неконечное значение")
            fixed = []
            for key, value, label in (("rf_hz", point["rf_hz"], f"RF {point['rf_hz'] / 1e6:g} МГц"),
                    ("rf_level", rf_level, f"RF/IF {rf_level:+g} dBm" if rf_level is not None else "RF/IF неизвестен"),
                    ("lo_level", lo_level, f"LO {lo_level:+g} dBm" if lo_level is not None else "LO неизвестен")):
                if key != x_axis:
                    fixed.append((key, value, label))
            profile = tuple(settings.get(key) for key in ("rbw_hz", "vbw_hz", "detector", "attenuation_db",
                "reference_dbm", "preamp", "amplitude_correction", "reference_offset_db", "external_gain_db", "points", "trace_type"))
            profile += (settings["stop_hz"] - settings["start_hz"], spectrum.get("averaging", {}).get("count", 1),
                        spectrum.get("averaging", {}).get("method", "none"))
            calibration = tuple(point.get(key, 0) for key in ("rf_loss_db", "lo_loss_db", "if_loss_db", "output_loss_db"))
            if y_axis == "corrected":
                calibration += (point.get("pin_known"), point.get("path_known"))
            key = (point.get("sample", ""), point.get("batch", ""), point.get("model", ""), method,
                   point.get("if_hz"), point["lo_hz"] - point["rf_hz"], tuple(value for _, value, _ in fixed),
                   profile, calibration, spectrum.get("identity"), spectrum.get("demo"),
                   spectrum.get("generator", {}).get("identity"), point.get("pin_dbm") if rf_level is None else None,
                   point.get("full_test_id"), point.get("rf_channel"), point.get("lo_channel"), point.get("sources"))
            if key not in groups:
                title = " · ".join([point.get("sample", ""), dict((key, label) for label, key in METHOD_CHOICES).get(method, method)]
                    + [label for _, _, label in fixed] + [f"ПЧ {point['if_hz'] / 1e6:g}",
                      f"ATT {settings.get('attenuation_db', '?')}", f"RBW {settings.get('rbw_hz', '?')}",
                      f"Ref {settings.get('reference_dbm', '?')}",
                      f"{settings.get('detector', '?')} ×{spectrum.get('averaging', {}).get('count', 1)} / {settings.get('points', '?')} т."])
                groups[key] = dict(name=title, sample=point.get("sample", ""), method=method, bins={})
            groups[key]["bins"].setdefault(x, []).append((y, spectrum["id"], spectrum.get("averaging", {}).get("marker_std_db", 0)))
        except (KeyError, ValueError, TypeError, IndexError):
            skipped += 1
    if len(groups) > 100:
        raise ValueError(f"Получилось {len(groups)} кривых. Сузьте фильтры архива по частоте, уровням RF/LO или образцу (не более 100 кривых).")
    result = []
    for group in groups.values():
        points = []
        for x, samples in sorted(group.pop("bins").items()):
            values = [value for value, _, _ in samples]
            points.append(dict(x=x, y=statistics.mean(values), n=len(values),
                               std_db=statistics.stdev(values) if len(values) > 1 else 0,
                               sweep_std_db=max(std for _, _, std in samples),
                               spectrum_ids=[identity for _, identity, _ in samples]))
        result.append(dict(group, points=points))
    return result, skipped


class ArchiveCharts(QtWidgets.QWidget):
    settings_changed = QtCore.Signal()

    def __init__(self, provider, parent=None):
        super().__init__(parent)
        self.provider = provider
        self.groups = []
        self.spectra = None
        layout = QtWidgets.QVBoxLayout(self)
        controls = QtWidgets.QFormLayout()
        layout.addLayout(controls)
        self.x_axis, self.y_axis, self.scope = QtWidgets.QComboBox(), QtWidgets.QComboBox(), QtWidgets.QComboBox()
        for label, key in X_AXES:
            self.x_axis.addItem(label, key)
        for label, key in Y_AXES:
            self.y_axis.addItem(label, key)
        self.scope.addItem("Все строки после фильтров архива", False)
        self.scope.addItem("Только выделенные строки архива", True)
        controls.addRow("Ось X", self.x_axis)
        controls.addRow("Ось Y", self.y_axis)
        controls.addRow("Данные", self.scope)
        self.show_spread = QtWidgets.QCheckBox("Показывать разброс повторов / развёрток")
        self.show_spread.setChecked(True)
        controls.addRow(self.show_spread)
        self.centers_only = QtWidgets.QCheckBox("Только центры диапазонов, как в обзорном графике отчёта")
        controls.addRow(self.centers_only)
        row = QtWidgets.QHBoxLayout()
        layout.addLayout(row)
        for title, callback in (("Построить", self.build_chart), ("Масштаб по данным", self.reset_scale),
                                ("Сохранить PNG…", self.export_png), ("Экспорт точек CSV…", self.export_csv)):
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(callback)
            row.addWidget(button)
        self.plot = pg.PlotWidget(background="#101923")
        self.plot.setMenuEnabled(False)
        self.plot.getViewBox().setMenuEnabled(False)
        self.plot.hideButtons()
        self.plot.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.plot.customContextMenuRequested.connect(self.show_plot_menu)
        for name in ("bottom", "left"):
            axis = self.plot.getAxis(name)
            axis.setPen("#8191a8")
            axis.setTextPen("#d8e4ef")
            axis.setSIPrefixEnableRanges(())
            axis.enableAutoSIPrefix(False)
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.legend = self.plot.addLegend(labelTextColor="#d8e4ef", brush="#182636dd", offset=(10, 10))
        self.point_hover = PointHover(self.plot, self.point_hint_text)
        self.update_axis_labels()
        layout.addWidget(self.plot, 1)
        self.status = QtWidgets.QLabel("Отфильтруйте архив по образцам, частотам и уровням, затем нажмите «Построить».")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        for widget in (self.x_axis, self.y_axis):
            widget.currentIndexChanged.connect(self.redraw)
            widget.currentIndexChanged.connect(lambda *args: self.settings_changed.emit())
        self.scope.currentIndexChanged.connect(self.invalidate)
        self.scope.currentIndexChanged.connect(lambda *args: self.settings_changed.emit())
        for widget in (self.show_spread, self.centers_only):
            widget.toggled.connect(self.redraw)
            widget.toggled.connect(lambda *args: self.settings_changed.emit())

    def settings(self):
        return dict(x=self.x_axis.currentData(), y=self.y_axis.currentData(), selected=self.scope.currentData(),
            spread=self.show_spread.isChecked(), centers=self.centers_only.isChecked())

    def restore(self, values):
        self.show_spread.setChecked(values.get("spread", True))
        self.centers_only.setChecked(values.get("centers", False))
        for widget, key in ((self.x_axis, "x"), (self.y_axis, "y"), (self.scope, "selected")):
            index = widget.findData(values.get(key))
            if index >= 0:
                widget.setCurrentIndex(index)

    def invalidate(self, *args):
        self.spectra = None
        self.clear_plot()
        self.status.setText("Набор данных изменился. Нажмите «Построить» для текущего выбора и фильтров.")

    def clear_plot(self):
        self.point_hover.hide()
        self.groups = []
        self.plot.clear()
        self.legend.clear()
        self.point_hover.restore_lines()

    def update_axis_labels(self):
        self.plot.setLabel("bottom", self.x_axis.currentText(), color="#d8e4ef")
        self.plot.setLabel("left", self.y_axis.currentText(), color="#d8e4ef")

    def build_chart(self):
        self.spectra = list(self.provider(self.scope.currentData()))
        self.redraw()

    def redraw(self, *args):
        self.update_axis_labels()
        if self.spectra is None:
            return
        self.clear_plot()
        spectra = self.spectra
        if self.centers_only.isChecked():
            spectra = [spectrum for spectrum in spectra if spectrum.get("context", {}).get("position") == "center"]
        try:
            self.groups, skipped = chart_groups(spectra, self.x_axis.currentData(), self.y_axis.currentData())
            if not self.groups:
                raise ValueError("Нет подходящих точек. Проверьте выбор, фильтры и вид Y. Для результата с поправками они должны быть подтверждены.")
            colors = ["#55c4df", "#6dd3a4", "#f4bd61", "#c49bf4", "#f08286", "#a9cd6c"]
            for index, group in enumerate(self.groups):
                color = colors[index % len(colors)]
                x, y = [p["x"] for p in group["points"]], [p["y"] for p in group["points"]]
                data = [dict(point, sample=group["sample"], method=group["method"], index=number + 1,
                    count=len(group["points"])) for number, point in enumerate(group["points"])]
                curve = self.plot.plot(x, y, data=data,
                    pen=pg.mkPen(color, width=2), symbol="o", symbolSize=8, symbolBrush=color, name=group["name"])
                self.point_hover.attach(curve, x, y, data)
                spreads = [max(p["std_db"], p["sweep_std_db"]) for p in group["points"]]
                if self.show_spread.isChecked() and any(spreads):
                    import numpy as np
                    self.plot.addItem(pg.ErrorBarItem(x=np.array([p["x"] for p in group["points"]]),
                        y=np.array([p["y"] for p in group["points"]]), height=2 * np.array(spreads), pen=pg.mkPen(color, width=1)))
            self.reset_scale()
            note = " Без кабельных поправок." if self.y_axis.currentData() == "raw_loss" else ""
            self.status.setText(f"Кривых: {len(self.groups)}; спектров: {len(spectra) - skipped}; пропущено: {skipped}. "
                "Разные условия и полные испытания не смешиваются. Повторы одного X усредняются. "
                "Усы: больший из σ повторов и σ развёрток; при одном замере разброс неизвестен." + note)
        except ValueError as error:
            self.status.setText(str(error))

    def point_hint_text(self, point):
        method = dict((key, label) for label, key in METHOD_CHOICES).get(point["method"], point["method"])
        return (f"{point['sample']} · {method}\nТочка {point['index']}/{point['count']}\n"
            f"X · {self.x_axis.currentText()}: {point['x']:.6g}\n"
            f"Y · {self.y_axis.currentText()}: {point['y']:.6g}\n"
            f"Повторов: {point['n']}\nσ повторов: {point['std_db']:.4g} дБ\n"
            f"σ развёрток: {point['sweep_std_db']:.4g} дБ")

    def create_plot_menu(self):
        menu = QtWidgets.QMenu(self)
        menu.setStyleSheet("""
            QMenu
            {
                background: #192536;
                color: #d8e4ef;
                border: 1px solid #405570;
                padding: 4px;
            }
            QMenu::item
            {
                padding: 5px 22px;
            }
            QMenu::item:selected
            {
                background: #305d70;
            }
            QMenu::item:disabled
            {
                color: #8191a8;
            }
        """)
        for title, callback in (("Масштаб по данным", self.reset_scale),
                ("Показывать разброс повторов / развёрток", self.show_spread.setChecked),
                ("Сохранить PNG…", self.export_png), ("Экспорт точек CSV…", self.export_csv)):
            action = menu.addAction(title)
            if title.startswith("Показывать"):
                action.setCheckable(True)
                action.setChecked(self.show_spread.isChecked())
                action.triggered.connect(lambda checked: self.show_spread.setChecked(checked))
            else:
                action.setEnabled(bool(self.groups))
                action.triggered.connect(callback)
        return menu

    def show_plot_menu(self, position):
        self.point_hover.hide()
        menu = self.create_plot_menu()
        menu.exec(self.plot.mapToGlobal(position))
        menu.deleteLater()

    def reset_scale(self):
        self.plot.enableAutoRange()

    def export_png(self):
        if not self.groups:
            self.status.setText("Сначала постройте график.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "График отчёта", "mixer-chart.png", "PNG (*.png)")
        if path:
            try:
                exporter = pg.exporters.ImageExporter(self.plot.plotItem)
                exporter.parameters()["width"] = 2400
                exporter.export(path)
                self.status.setText("График сохранён: " + path)
            except Exception as error:
                self.status.setText("Ошибка экспорта: " + str(error))

    def export_csv(self):
        if not self.groups:
            self.status.setText("Сначала постройте график.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Точки графика", "mixer-chart.csv", "CSV (*.csv)")
        if path:
            try:
                with open(path, "w", encoding="utf-8-sig", newline="") as stream:
                    writer = csv.writer(stream, delimiter=";")
                    writer.writerow(["Кривая", self.x_axis.currentText(), self.y_axis.currentText(), "Повторов", "σ повторов, дБ", "σ развёрток, дБ", "UUID спектров"])
                    for group in self.groups:
                        for point in group["points"]:
                            writer.writerow([group["name"], point["x"], point["y"], point["n"], point["std_db"], point["sweep_std_db"], json.dumps(point["spectrum_ids"])])
                self.status.setText("Точки графика сохранены: " + path)
            except OSError as error:
                self.status.setText("Ошибка экспорта: " + str(error))
