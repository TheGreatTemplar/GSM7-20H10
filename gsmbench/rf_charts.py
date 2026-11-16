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
                "reference_dbm", "preamp", "amplitude_correction", "reference_offset_db", "external_gain_db"))
            calibration = tuple(point.get(key) for key in ("rf_loss_db", "lo_loss_db", "if_loss_db", "output_loss_db",
                                                           "pin_known", "path_known")) if y_axis == "corrected" else ()
            key = (point.get("sample", ""), point.get("batch", ""), point.get("model", ""), method,
                   point.get("if_hz"), point["lo_hz"] - point["rf_hz"], tuple(value for _, value, _ in fixed),
                   profile, calibration, spectrum.get("identity"), spectrum.get("demo"),
                   spectrum.get("generator", {}).get("identity"), point.get("pin_dbm") if rf_level is None else None)
            if key not in groups:
                title = " · ".join([point.get("sample", ""), dict((key, label) for label, key in METHOD_CHOICES).get(method, method)]
                    + [label for _, _, label in fixed] + [f"ПЧ {point['if_hz'] / 1e6:g}",
                      f"ATT {settings.get('attenuation_db', '?')}", f"RBW {settings.get('rbw_hz', '?')}",
                      f"Ref {settings.get('reference_dbm', '?')}"])
                groups[key] = dict(name=title, sample=point.get("sample", ""), method=method, bins={})
            groups[key]["bins"].setdefault(x, []).append((y, spectrum["id"]))
        except (KeyError, ValueError, TypeError, IndexError):
            skipped += 1
    if len(groups) > 100:
        raise ValueError(f"Получилось {len(groups)} кривых. Сузьте фильтры архива по частоте, уровням RF/LO или образцу (не более 100 кривых).")
    result = []
    for group in groups.values():
        points = []
        for x, samples in sorted(group.pop("bins").items()):
            values = [value for value, _ in samples]
            points.append(dict(x=x, y=statistics.mean(values), n=len(values),
                               std_db=statistics.stdev(values) if len(values) > 1 else 0,
                               spectrum_ids=[identity for _, identity in samples]))
        result.append(dict(group, points=points))
    return result, skipped


class ArchiveCharts(QtWidgets.QWidget):
    settings_changed = QtCore.Signal()

    def __init__(self, provider, parent=None):
        super().__init__(parent)
        self.provider = provider
        self.groups = []
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
        row = QtWidgets.QHBoxLayout()
        layout.addLayout(row)
        for title, callback in (("Построить", self.build_chart), ("Масштаб по данным", self.reset_scale),
                                ("Сохранить PNG…", self.export_png), ("Экспорт точек CSV…", self.export_csv)):
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(callback)
            row.addWidget(button)
        self.plot = pg.PlotWidget(background="#f7faff")
        for name in ("bottom", "left"):
            axis = self.plot.getAxis(name)
            axis.setPen("#223247")
            axis.setTextPen("#223247")
            axis.setSIPrefixEnableRanges(())
            axis.enableAutoSIPrefix(False)
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.legend = self.plot.addLegend(labelTextColor="#223247", brush="#f7faffdd", offset=(10, 10))
        layout.addWidget(self.plot, 1)
        self.status = QtWidgets.QLabel("Отфильтруйте архив по образцам, частотам и уровням, затем нажмите «Построить».")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        for widget in (self.x_axis, self.y_axis, self.scope):
            widget.currentIndexChanged.connect(self.invalidate)
            widget.currentIndexChanged.connect(lambda *args: self.settings_changed.emit())

    def settings(self):
        return dict(x=self.x_axis.currentData(), y=self.y_axis.currentData(), selected=self.scope.currentData())

    def restore(self, values):
        for widget, key in ((self.x_axis, "x"), (self.y_axis, "y"), (self.scope, "selected")):
            index = widget.findData(values.get(key))
            if index >= 0:
                widget.setCurrentIndex(index)

    def invalidate(self, *args):
        self.groups = []
        self.plot.clear()
        self.legend.clear()
        self.status.setText("Данные или настройки изменились. Нажмите «Построить» для текущего выбора и фильтров.")

    def build_chart(self):
        self.invalidate()
        spectra = self.provider(self.scope.currentData())
        try:
            self.groups, skipped = chart_groups(spectra, self.x_axis.currentData(), self.y_axis.currentData())
            if not self.groups:
                raise ValueError("Нет подходящих точек. Проверьте выбор, фильтры и вид Y. Для результата с поправками они должны быть подтверждены.")
            colors = ["#245b9e", "#168477", "#c17a16", "#8b508c", "#c54c45", "#42792f"]
            for index, group in enumerate(self.groups):
                color = colors[index % len(colors)]
                self.plot.plot([p["x"] for p in group["points"]], [p["y"] for p in group["points"]],
                    pen=pg.mkPen(color, width=2), symbol="o", symbolSize=6, symbolBrush=color, name=group["name"])
            self.plot.setLabel("bottom", self.x_axis.currentText(), color="#223247")
            self.plot.setLabel("left", self.y_axis.currentText(), color="#223247")
            self.reset_scale()
            note = " Без кабельных поправок." if self.y_axis.currentData() == "raw_loss" else ""
            self.status.setText(f"Кривых: {len(self.groups)}; спектров: {len(spectra) - skipped}; пропущено: {skipped}. "
                "Разные частоты, уровни и настройки не смешиваются. Повторы одного X при одинаковых условиях усредняются." + note)
        except ValueError as error:
            self.status.setText(str(error))

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
                    writer.writerow(["Кривая", self.x_axis.currentText(), self.y_axis.currentText(), "Повторов", "Стандартное отклонение", "UUID спектров"])
                    for group in self.groups:
                        for point in group["points"]:
                            writer.writerow([group["name"], point["x"], point["y"], point["n"], point["std_db"], json.dumps(point["spectrum_ids"])])
                self.status.setText("Точки графика сохранены: " + path)
            except OSError as error:
                self.status.setText("Ошибка экспорта: " + str(error))
