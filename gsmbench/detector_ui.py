"""Dedicated LO power-detector calibration workspace."""

import copy
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
import threading
import uuid

from PySide6 import QtCore, QtWidgets
import pyqtgraph as pg
import pyqtgraph.exporters

from .detector import DetectorPlan, DetectorStore, SOURCE_FACTORIES, voltage_to_dbm
from .hp34401 import Hp34401a
from .dmm6500 import Dmm6500
from .form_layout import compact_inputs
from .validation import FieldError, FieldFeedback, StatusLabel
from .rf_labels import SETTLING_TIME
from .rsa import AcquisitionCancelled
from .table_filters import FilterHeader


class DetectorWorker(QtCore.QThread):
    log = QtCore.Signal(str)
    status = QtCore.Signal(str)
    updated = QtCore.Signal(object)
    outputs = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, owner, settings, plan, operation):
        super().__init__(owner)
        self.settings, self.plan, self.operation = settings, plan, operation
        self.stop = threading.Event()

    def run(self):
        settings, plan = self.settings, self.plan
        source, meter, run = None, None, None
        owns = self.operation in ("point", "series", "off")
        enable_attempted = False
        try:
            if self.operation != "meter" and not settings["demo"]:
                source = SOURCE_FACTORIES[settings["source"]](settings, self.log.emit)
            if self.operation == "source":
                state = source.state() if source else dict(identity="SIMULATOR", output=0)
                self.status.emit(json.dumps(state, ensure_ascii=False))
                self.outputs.emit(dict(known=True, active=bool(state["output"])))
                return
            if self.operation == "off":
                return
            if not settings["demo"]:
                meter = (Dmm6500(settings["dmm_host"], settings["dmm_port"], self.log.emit)
                    if settings.get("meter_kind") == "dmm6500" else
                    Hp34401a(settings["com_port"], settings["baud"], self.log.emit))
            if self.operation == "meter":
                config = meter.configure_dc(plan.nplc, plan.voltage_range, plan.high_impedance, plan.digits) if meter else {}
                self.status.emit((meter.identity if meter else "SIMULATOR")
                    + f" · NPLC {config.get('nplc', plan.nplc):g}"
                    + (f" · разрядность дисплея {config['display_digits']:g}" if "display_digits" in config else
                        f" · разрешение {config.get('actual_resolution_v')} В"))
                return
            levels = plan.levels(single=self.operation == "point")
            self.status.emit("Подготовка источника и вольтметра; RF OFF")
            if source:
                source.prepare(plan.frequency_hz, levels[0])
                for level in levels:
                    if self.stop.is_set():
                        raise AcquisitionCancelled()
                    state = source.setpoint(plan.frequency_hz, level)
                    if state["output"]:
                        raise ValueError("Предварительная проверка уровней требует RF OFF")
            meter_settings = meter.configure_dc(plan.nplc, plan.voltage_range, plan.high_impedance, plan.digits) if meter else dict(
                identity="SIMULATOR," + ("DMM6500" if settings.get("meter_kind") == "dmm6500" else "34401A"), **asdict(plan))
            run = dict(id=str(uuid.uuid4()), measured_at=datetime.now(timezone.utc).isoformat(), demo=settings["demo"],
                sample=settings["sample"], notes=settings["notes"], source_kind=settings["source"],
                meter_kind=settings.get("meter_kind", "hp34401a"),
                source_endpoint={key: settings[key] for key in ("akip_host", "akip_port", "smu_host", "smu_port")},
                meter=meter_settings, plan=asdict(plan), points=[], state="running",
                calibration_basis="generator_readback_and_user_entered_attenuation", operation=self.operation)
            self.status.emit("Измерение фона при RF OFF")
            zero = self.readings(meter, None)
            run["zero_readings_v"], run["zero_voltage_v"] = zero, statistics.mean(zero)
            active = False
            for index, level in enumerate(levels):
                if self.stop.is_set():
                    raise AcquisitionCancelled()
                state = source.setpoint(plan.frequency_hz, level) if source else dict(
                    identity="SIMULATOR," + settings["source"], output=1, frequency_hz=plan.frequency_hz, level_dbm=level)
                if source and not active:
                    enable_attempted = True
                    state = source.enable(self.stop)
                    active = True
                if self.stop.wait(plan.settling_s):
                    raise AcquisitionCancelled()
                self.status.emit(f"Точка {index + 1}/{len(levels)}: {level:+g} dBm")
                readings = self.readings(meter, level)
                if source and source.state() != state:
                    raise ValueError("Состояние источника изменилось во время измерения")
                point = dict(index=index + 1, set_dbm=level, actual_dbm=state["level_dbm"],
                    input_dbm=state["level_dbm"] - plan.input_attenuation_db,
                    voltage_v=statistics.mean(readings), std_v=statistics.stdev(readings) if len(readings) > 1 else 0,
                    readings_v=readings, source=state, measured_at=datetime.now(timezone.utc).isoformat())
                run["points"].append(point)
                self.save(run)
                self.updated.emit(copy.deepcopy(run))
            run["state"] = "complete"
        except (AcquisitionCancelled, InterruptedError):
            if run:
                run["state"] = "cancelled"
            self.status.emit("Остановлено; отключаю RF")
        except Exception as error:
            if run:
                run.update(state="failed", error=str(error))
            self.failed.emit(str(error))
            if self.operation == "source":
                self.outputs.emit(dict(known=False, active=False, requires_off=False))
        finally:
            off_error = None
            if owns:
                if source:
                    try:
                        source.off()
                    except Exception as error:
                        off_error = str(error)
                elif not settings["demo"]:
                    off_error = "Связь с источником не установлена; RF OFF не подтверждён"
                must_confirm_off = enable_attempted or self.operation == "off"
                if off_error:
                    self.outputs.emit(dict(known=False, active=must_confirm_off, requires_off=must_confirm_off))
                else:
                    self.outputs.emit(dict(known=True, active=False))
                if off_error and must_confirm_off:
                    self.failed.emit("RF OFF не подтверждён: " + off_error)
                elif off_error:
                    self.log.emit("Включение RF не выполнялось; отсутствие связи не блокирует настройки и повторное подключение.")
            if run:
                run["rf_off_confirmed"] = off_error is None
                if off_error:
                    run.update(state="failed", off_error=off_error)
                try:
                    self.save(run)
                    self.updated.emit(copy.deepcopy(run))
                except Exception as error:
                    self.failed.emit("Сохранение серии: " + str(error))
            for device in (meter, source):
                if device:
                    try:
                        device.close()
                    except Exception as error:
                        self.failed.emit("Закрытие соединения: " + str(error))

    def readings(self, meter, level):
        result = []
        for index in range(self.plan.samples):
            if self.stop.is_set():
                raise AcquisitionCancelled()
            if meter:
                value = meter.read_voltage(self.stop)
            else:
                signal = 0 if level is None else 0.02 * 10 ** ((level - self.plan.input_attenuation_db) / 20)
                value = 0.002 + signal + (index - (self.plan.samples - 1) / 2) * 1e-6
            result.append(value)
        return result

    def save(self, run):
        pending = Path(self.settings["root"]) / "data" / "detector-pending" / (run["id"] + ".json")
        pending.parent.mkdir(parents=True, exist_ok=True)
        temporary = pending.with_suffix(".tmp")
        temporary.write_text(json.dumps(dict(database=self.settings["database"], run=run), ensure_ascii=False, allow_nan=False), encoding="utf-8")
        temporary.replace(pending)
        DetectorStore(self.settings["database"], self.settings["demo"]).save(run)
        pending.unlink()


class DetectorPlot(pg.PlotWidget):
    """Point hover overlay without the timeout of a native tooltip."""

    def __init__(self):
        super().__init__()
        self.hover_point = None
        self.hover_label = QtWidgets.QLabel(self.viewport())
        self.hover_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.hover_label.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hover_label.setStyleSheet("background: #252d38; color: #f0f4fa; border: 1px solid #8496aa; padding: 6px;")
        self.hover_label.hide()
        self.hover_lines = [QtWidgets.QGraphicsLineItem(), QtWidgets.QGraphicsLineItem()]
        for line in self.hover_lines:
            line.setPen(pg.mkPen("#aebccc", style=QtCore.Qt.PenStyle.DashLine))
            line.setZValue(20)
            line.hide()
            self.addItem(line, ignoreBounds=True)
        self.viewport().installEventFilter(self)
        self.getViewBox().sigRangeChanged.connect(self.position_hover)
        self.getViewBox().sigResized.connect(self.position_hover)

    def clear_points(self):
        self.hide_hover()
        self.plotItem.clear()
        for line in self.hover_lines:
            self.addItem(line, ignoreBounds=True)

    def set_points(self, points, frequency_hz):
        self.clear_points()
        self.frequency_hz = frequency_hz
        self.point_count = len(points)
        curve = self.plot([point["input_dbm"] for point in points], [point["voltage_v"] for point in points],
            data=points, pen=pg.mkPen("#45d7d0", width=2), symbol="o", symbolSize=8)
        x, y = curve.getData()
        curve.scatter.setData(x=x, y=y, data=points, hoverable=True, tip=None,
            hoverSize=12, hoverBrush=pg.mkBrush("#f4bd61"))
        curve.scatter.sigHovered.connect(self.hover_points)

    def hover_points(self, item, points, event):
        if len(points) == 0:
            self.hide_hover()
            return
        self.hover_point = points[0].data()
        point = self.hover_point
        self.hover_label.setText(f"Точка {point['index']}/{self.point_count} · LO {self.frequency_hz / 1e6:g} МГц\n"
            f"На детекторе: {point['input_dbm']:+.3f} dBm\n"
            f"U = {pg.siFormat(point['voltage_v'], precision=6, suffix='V')}")
        self.hover_label.adjustSize()
        self.hover_label.show()
        self.hover_label.raise_()
        self.position_hover()

    def position_hover(self, *args):
        if self.hover_point is None:
            return
        x, y = self.hover_point["input_dbm"], self.hover_point["voltage_v"]
        (left, right), (bottom, top) = self.getViewBox().viewRange()
        self.hover_lines[0].setLine(x, bottom, x, y)
        self.hover_lines[1].setLine(left, y, x, y)
        for line in self.hover_lines:
            line.show()
        position = self.mapFromScene(self.getViewBox().mapViewToScene(QtCore.QPointF(x, y)))
        width, height = self.hover_label.width(), self.hover_label.height()
        viewport = self.viewport()
        left = position.x() + 18
        if left + width > viewport.width() - 4:
            left = position.x() - width - 18
        top = position.y() - height - 18
        if top < 4:
            top = position.y() + 18
        self.hover_label.move(max(4, min(left, viewport.width() - width - 4)),
            max(4, min(top, viewport.height() - height - 4)))

    def hide_hover(self):
        self.hover_point = None
        self.hover_label.hide()
        for line in self.hover_lines:
            line.hide()

    def eventFilter(self, watched, event):
        if watched is self.viewport() and event.type() in (QtCore.QEvent.Type.Leave, QtCore.QEvent.Type.Hide):
            self.hide_hover()
        return super().eventFilter(watched, event)


class DetectorPanel(QtWidgets.QWidget):
    settings_changed = QtCore.Signal(object)

    def __init__(self, owner, root, settings):
        super().__init__(owner)
        self.owner, self.root = owner, Path(root)
        self.worker = None
        self.unsafe_outputs = False
        self.last_settings = None
        self.last_error = None
        self.current = None
        self.runs = []
        self.column_filters = {}
        self.fields = {}
        layout = QtWidgets.QHBoxLayout(self)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(330)
        scroll.setMaximumWidth(420)
        layout.addWidget(scroll)
        self.form_widget = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(self.form_widget)
        self.form = form
        form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        scroll.setWidget(self.form_widget)
        right = QtWidgets.QVBoxLayout()
        layout.addLayout(right, 1)

        def add(key, title, widget):
            self.fields[key] = widget
            form.addRow(title, widget)
            return widget

        def number(value, low, high, decimals=2):
            widget = QtWidgets.QDoubleSpinBox()
            widget.setRange(low, high)
            widget.setDecimals(decimals)
            widget.setValue(value)
            return widget

        def combo(items):
            widget = QtWidgets.QComboBox()
            for title, data in items:
                widget.addItem(title, data)
            return widget

        add("demo", "Режим", QtWidgets.QCheckBox("Демо без приборов")).setChecked(True)
        add("sample", "Детектор", QtWidgets.QLineEdit("LODET001"))
        add("notes", "Примечание", QtWidgets.QLineEdit())
        add("source", "Источник", combo([("АКИП-3208", "akip"), ("SMU200A · A/1", "smu_a"), ("SMU200A · B/2", "smu_b")]))
        add("akip_host", "IP АКИП", QtWidgets.QLineEdit("172.16.236.190"))
        add("akip_port", "TCP АКИП", number(5025, 1, 65535, 0))
        add("smu_host", "IP SMU", QtWidgets.QLineEdit("169.254.234.52"))
        add("smu_port", "TCP SMU", number(5025, 1, 65535, 0))
        add("meter_kind", "Вольтметр", combo([("HP 34401A · RS-232", "hp34401a"), ("Keithley DMM6500 · Ethernet", "dmm6500")]))
        add("dmm_host", "IP DMM6500", QtWidgets.QLineEdit("172.16.236.45"))
        add("dmm_port", "TCP DMM6500", number(5025, 1, 65535, 0))
        add("com_port", "34401A · COM", QtWidgets.QLineEdit("COM3"))
        add("baud", "Скорость · 8N2", combo([(str(baud), baud) for baud in (300, 600, 1200, 2400, 4800, 9600)])).setCurrentIndex(5)
        add("frequency_mhz", "Частота LO, МГц", number(14, 0.1, 3000, 6))
        add("start_dbm", "Первый / одиночный, dBm", number(-30, -110, 20))
        add("stop_dbm", "Конечный уровень, dBm", number(10, -110, 20))
        add("step_db", "Шаг серии, дБ", number(1, 0.01, 20))
        add("attenuation_db", "Ослабление до детектора, дБ", number(0, 0, 100))
        add("settling_s", SETTLING_TIME, number(0.5, 0, 60))
        add("samples", "Отсчётов на точку", number(3, 1, 100, 0))
        add("nplc", "Вольтметр · NPLC", combo([(str(value), value) for value in (0.02, 0.2, 1, 10, 100)])).setCurrentIndex(3)
        add("digits", "Вольтметр · разрядность", combo([(text, value) for text, value in (("4,5", 4.5), ("5,5", 5.5), ("6,5", 6.5))])).setCurrentIndex(2)
        add("voltage_range", "Диапазон DC V", combo([("Авто", 0)] + [(f"{value:g} В", value) for value in (0.1, 1, 10, 100, 1000)])).setCurrentIndex(3)
        impedance = add("high_impedance", "Входное сопротивление", combo([
            ("10 МОм", False), ("Высокое, >10 ГОм (авто)", True)]))
        impedance.setCurrentIndex(1)
        impedance.setToolTip("Высокое сопротивление доступно на диапазонах 0,1; 1 и 10 В. На 100 и 1000 В — 10 МОм.")
        hint = QtWidgets.QLabel("Источник → аттенюаторы/кабель → RF-вход детектора. DC-выход детектора → V/COM вольтметра. "
            "Ослабление задаётся суммарно до детектора. Неиспользуемый канал SMU выключен. "
            "Данные основаны на показании генератора и введённом ослаблении.")
        hint.setWordWrap(True)
        form.addRow(hint)
        connection = QtWidgets.QHBoxLayout()
        form.addRow(connection)
        self.connection_buttons = []
        for title, operation in (("Источник", "source"), ("вольтметр", "meter")):
            button = QtWidgets.QPushButton("Проверить " + title)
            button.clicked.connect(lambda checked=False, operation=operation: self.launch(operation))
            self.connection_buttons.append(button)
            connection.addWidget(button)
        self.wiring_box = QtWidgets.QFrame()
        self.wiring_box.setObjectName("detectorWiring")
        wiring_layout = QtWidgets.QVBoxLayout(self.wiring_box)
        self.wiring = QtWidgets.QCheckBox("Схема подключения проверена")
        wiring_layout.addWidget(self.wiring)
        wiring_hint = QtWidgets.QLabel("Выбранный выход генератора → RF-вход детектора; DC-выход детектора → V/COM выбранного вольтметра.")
        wiring_hint.setWordWrap(True)
        wiring_layout.addWidget(wiring_hint)
        self.wiring.toggled.connect(self.update_wiring_style)
        self.update_wiring_style()
        right.addWidget(self.wiring_box)
        actions = QtWidgets.QHBoxLayout()
        right.addLayout(actions)
        self.measure_buttons = []
        for title, operation in (("Измерить точку", "point"), ("Снять калибровочную кривую", "series")):
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(lambda checked=False, operation=operation: self.launch(operation))
            self.measure_buttons.append(button)
            actions.addWidget(button)
        stop = QtWidgets.QPushButton("СТОП / RF OFF")
        stop.clicked.connect(self.stop)
        actions.addWidget(stop)
        self.manual_off_button = QtWidgets.QPushButton("Подтвердить RF OFF вручную")
        self.manual_off_button.setToolTip("Нажмите только после физической проверки: выбранный источник выключен или его RF-выход отключён.")
        self.manual_off_button.clicked.connect(self.confirm_manual_off)
        self.manual_off_button.hide()
        right.addWidget(self.manual_off_button)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        right.addWidget(splitter, 1)
        self.plot = DetectorPlot()
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.setLabel("bottom", "Уровень на детекторе", units="dBm")
        self.plot.setLabel("left", "Напряжение детектора", units="V")
        splitter.addWidget(self.plot)
        tabs = QtWidgets.QTabWidget()
        splitter.addWidget(tabs)
        splitter.setSizes([420, 280])
        self.points = QtWidgets.QTableWidget(0, 6)
        self.points.setHorizontalHeaderLabels(["№", "Задано, dBm", "На детекторе, dBm", "U, В", "σ, В", "Отсчётов"])
        self.points.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.points.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        tabs.addTab(self.points, "Точки")
        archive = QtWidgets.QWidget()
        archive_layout = QtWidgets.QVBoxLayout(archive)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Поиск калибровок…")
        self.search.setClearButtonEnabled(True)
        archive_layout.addWidget(self.search)
        self.archive_headers = ["Детектор", "Дата UTC", "Источник", "LO, МГц", "Точек", "Статус", "Примечание"]
        self.archive = QtWidgets.QTableWidget(0, len(self.archive_headers))
        self.archive.setHorizontalHeader(FilterHeader(self.archive))
        self.archive.setHorizontalHeaderLabels(self.archive_headers)
        self.archive.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.archive.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.archive.horizontalHeader().sectionClicked.connect(self.show_filter)
        self.archive.itemSelectionChanged.connect(self.select_run)
        archive_layout.addWidget(self.archive)
        archive_buttons = QtWidgets.QHBoxLayout()
        archive_layout.addLayout(archive_buttons)
        for title, callback in (("Обновить", self.reload), ("Сбросить фильтры", self.clear_filters), ("Восстановить", self.recover)):
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(callback)
            archive_buttons.addWidget(button)
        tabs.addTab(archive, "Архив калибровок")
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        tabs.addTab(self.log, "Журнал")
        convert = QtWidgets.QHBoxLayout()
        right.addLayout(convert)
        convert.addWidget(QtWidgets.QLabel("Напряжение, В:"))
        self.convert_voltage = number(0, -1000, 1000, 7)
        convert.addWidget(self.convert_voltage)
        self.convert_result = QtWidgets.QLabel("Выберите кривую")
        button = QtWidgets.QPushButton("Пересчитать в dBm")
        button.clicked.connect(self.convert)
        convert.addWidget(button)
        convert.addWidget(self.convert_result, 1)
        exports = QtWidgets.QHBoxLayout()
        right.addLayout(exports)
        for title, callback in (("Масштаб по данным", lambda: self.plot.enableAutoRange()), ("CSV…", self.export_csv),
                ("PNG…", self.export_png), ("Экспорт JSON…", self.export_json), ("Импорт JSON…", self.import_json)):
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(callback)
            exports.addWidget(button)
        self.status = StatusLabel("Готово. Подключение к приборам — только по кнопке.")
        self.status.setWordWrap(True)
        right.addWidget(self.status)
        for key, value in settings.items():
            if key in self.fields:
                self.set_value(self.fields[key], value)
        for widget in self.fields.values():
            signal = widget.toggled if isinstance(widget, QtWidgets.QCheckBox) else widget.currentIndexChanged if isinstance(widget, QtWidgets.QComboBox) else widget.valueChanged if isinstance(widget, QtWidgets.QDoubleSpinBox) else widget.textChanged
            signal.connect(self.changed)
        self.fields["demo"].toggled.connect(self.reload)
        self.fields["source"].currentIndexChanged.connect(self.update_source_fields)
        self.fields["meter_kind"].currentIndexChanged.connect(self.update_meter_fields)
        self.search.textChanged.connect(self.filter_archive)
        self.update_source_fields()
        self.update_meter_fields()
        compact_inputs(self.form_widget)
        self.feedback = FieldFeedback(dict(self.fields, wiring=self.wiring))
        self.reload()

    def update_meter_fields(self, *args):
        network = self.fields["meter_kind"].currentData() == "dmm6500"
        for key in ("dmm_host", "dmm_port"):
            self.form.setRowVisible(self.fields[key], network)
        for key in ("com_port", "baud"):
            self.form.setRowVisible(self.fields[key], not network)
        nplc = self.fields["nplc"]
        index = nplc.findData(100)
        nplc.model().item(index).setEnabled(not network)
        if network and nplc.currentData() == 100:
            nplc.setCurrentIndex(nplc.findData(10))
        self.fields["digits"].setToolTip("DMM6500: число знаков на дисплее; точность определяется NPLC. В БД сохраняется полный ответ прибора."
            if network else "34401A: разрешение задаётся вместе с фиксированным диапазоном DC V.")

    def update_source_fields(self, *args):
        akip = self.fields["source"].currentData() == "akip"
        for key in ("akip_host", "akip_port"):
            self.form.setRowVisible(self.fields[key], akip)
        for key in ("smu_host", "smu_port"):
            self.form.setRowVisible(self.fields[key], not akip)

    def set_value(self, widget, value):
        if isinstance(widget, QtWidgets.QCheckBox):
            widget.setChecked(bool(value))
        elif isinstance(widget, QtWidgets.QComboBox):
            index = widget.findData(value)
            if index >= 0:
                widget.setCurrentIndex(index)
        elif isinstance(widget, QtWidgets.QDoubleSpinBox):
            widget.setValue(value)
        else:
            widget.setText(str(value))

    def settings(self):
        return {key: widget.isChecked() if isinstance(widget, QtWidgets.QCheckBox) else widget.currentData()
            if isinstance(widget, QtWidgets.QComboBox) else widget.value() if isinstance(widget, QtWidgets.QDoubleSpinBox)
            else widget.text().strip() for key, widget in self.fields.items()}

    def update_wiring_style(self, *args):
        color = "#669b7b" if self.wiring.isChecked() else "#c59b51"
        self.wiring_box.setStyleSheet(f"QFrame#detectorWiring {{ border: 2px solid {color}; border-radius: 5px; }}"
            "QCheckBox { font-weight: bold; padding: 3px; } QCheckBox::indicator { width: 20px; height: 20px; }"
            "QCheckBox::indicator:unchecked { border: 2px solid #c59b51; border-radius: 3px; background: #243444; }")

    def changed(self, *args):
        connection_keys = {"demo", "source", "meter_kind", "attenuation_db"}
        connection_keys.update(("dmm_host", "dmm_port") if self.fields["meter_kind"].currentData() == "dmm6500"
            else ("com_port", "baud"))
        connection_keys.update(("akip_host", "akip_port") if self.fields["source"].currentData() == "akip"
            else ("smu_host", "smu_port"))
        if any(self.sender() is self.fields[key] for key in connection_keys):
            self.wiring.setChecked(False)
        if self.sender() is self.fields["demo"]:
            self.current = None
            self.plot.clear_points()
            self.points.setRowCount(0)
            self.convert_result.setText("Выберите кривую")
        self.settings_changed.emit(self.settings())

    def store(self):
        demo = self.fields["demo"].isChecked()
        return DetectorStore(self.root / "data" / ("lo-detectors-demo.db" if demo else "lo-detectors.db"), demo)

    def launch(self, operation):
        if self.worker:
            return
        self.feedback.clear()
        try:
            if self.unsafe_outputs and operation != "off":
                raise ValueError("Сначала подтвердите RF OFF кнопкой СТОП")
            if self.owner.rf_workspace_busy():
                raise ValueError("Сначала завершите измерения смесителя и отключите его RF-выходы")
            settings = self.settings()
            settings.update(root=str(self.root), database=str(self.store().path))
            for key in ("akip_port", "smu_port", "dmm_port", "samples"):
                settings[key] = int(settings[key])
            if operation == "off" and self.last_settings:
                settings = self.last_settings
            plan = DetectorPlan(frequency_hz=settings["frequency_mhz"] * 1e6, start_dbm=settings["start_dbm"],
                stop_dbm=settings["stop_dbm"], step_db=settings["step_db"], input_attenuation_db=settings["attenuation_db"],
                settling_s=settings["settling_s"], samples=settings["samples"], nplc=settings["nplc"],
                voltage_range=settings["voltage_range"], high_impedance=settings["high_impedance"])
            plan = DetectorPlan(**dict(asdict(plan), digits=settings["digits"]))
            if operation in ("point", "series"):
                plan.levels(single=operation == "point")
                if not settings["sample"]:
                    raise FieldError("Укажите номер детектора", "sample")
                if not settings["demo"] and not self.wiring.isChecked():
                    self.wiring.setFocus()
                    raise FieldError("Подключите источник к RF-входу детектора, DC-выход к V/COM выбранного вольтметра и отметьте «Схема подключения проверена» над кнопками измерения", "wiring")
            self.last_settings = settings
            self.last_error = None
            self.worker = DetectorWorker(self, settings, plan, operation)
            self.worker.log.connect(self.log.appendPlainText)
            self.worker.status.connect(self.status.setText)
            self.worker.failed.connect(self.error)
            self.worker.outputs.connect(self.outputs_changed)
            self.worker.updated.connect(self.show_run)
            self.worker.finished.connect(self.finished)
            self.set_busy(True)
            self.worker.start()
        except Exception as error:
            self.error(error)

    def set_busy(self, busy):
        self.form_widget.setEnabled(not busy)
        self.wiring_box.setEnabled(not busy)
        for button in self.measure_buttons:
            button.setEnabled(not busy)
        self.owner.workspaces.setTabEnabled(1, not busy)
        self.manual_off_button.setVisible(self.unsafe_outputs)
        self.manual_off_button.setEnabled(self.worker is None and self.unsafe_outputs)

    def outputs_changed(self, state):
        self.unsafe_outputs = state.get("requires_off", not state["known"] or state["active"])

    def confirm_manual_off(self):
        if self.worker is not None:
            return
        self.unsafe_outputs = False
        self.set_busy(False)
        self.log.appendPlainText("Пользователь подтвердил физическое отключение RF; блокировка снята без команды прибору.")
        self.status.setText("RF OFF подтверждён пользователем. Можно менять настройки и повторять подключение.")

    def error(self, message):
        self.feedback.show(message)
        message = str(message)
        self.last_error = message
        self.status.setText("ОШИБКА: " + message)
        self.log.appendPlainText("ОШИБКА: " + message)

    def stop(self):
        if self.worker:
            self.worker.stop.set()
        elif self.unsafe_outputs:
            self.launch("off")

    def finished(self):
        operation = self.worker.operation
        self.worker.deleteLater()
        self.worker = None
        self.set_busy(self.unsafe_outputs)
        self.reload()
        if self.unsafe_outputs:
            self.status.setText("RF OFF не подтверждён. Проверьте источник и повторите СТОП.")
        elif self.last_error:
            self.status.setText("ОШИБКА: " + self.last_error)
        elif operation in ("point", "series") and self.current and self.current["state"] == "complete":
            self.status.setText(f"Сохранено точек: {len(self.current['points'])}. RF OFF подтверждён. БД: {self.store().path}")

    def show_run(self, run):
        self.current = run
        points = run["points"]
        self.plot.set_points(points, run["plan"]["frequency_hz"])
        self.plot.setTitle(f"{run['sample']} · {run['plan']['frequency_hz'] / 1e6:g} МГц · "
            + ("ДЕМО" if run["demo"] else "по уровню генератора"))
        self.points.setRowCount(len(points))
        for row, point in enumerate(points):
            values = [str(point["index"]), f"{point['set_dbm']:.3f}", f"{point['input_dbm']:.3f}",
                f"{point['voltage_v']:.8g}", f"{point['std_v']:.3g}", str(len(point["readings_v"]))]
            for column, value in enumerate(values):
                self.points.setItem(row, column, QtWidgets.QTableWidgetItem(value))
        self.convert_result.setText("Выбрана кривая: " + run["sample"])

    def reload(self, *args):
        try:
            self.runs = self.store().load()
            blocker = QtCore.QSignalBlocker(self.archive)
            self.archive.setRowCount(len(self.runs))
            for row, run in enumerate(self.runs):
                values = [run["sample"], run["measured_at"][:19], run["source_kind"],
                    f"{run['plan']['frequency_hz'] / 1e6:.6f}", str(len(run["points"])), run["state"], run["notes"]]
                for column, value in enumerate(values):
                    self.archive.setItem(row, column, QtWidgets.QTableWidgetItem(value))
            del blocker
            self.filter_archive()
        except Exception as error:
            self.status.setText("Ошибка архива: " + str(error))

    def select_run(self):
        if self.worker:
            return
        selected = self.archive.selectionModel().selectedRows()
        if selected:
            self.show_run(self.runs[selected[0].row()])

    def show_filter(self, column):
        from .ui import ColumnFilterDialog
        values = {self.archive.item(row, column).text() for row in range(self.archive.rowCount())}
        dialog = ColumnFilterDialog(self.archive_headers[column], sorted(values), self.column_filters.get(column), self)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            selected = dialog.selected_values()
            if selected == values:
                self.column_filters.pop(column, None)
            else:
                self.column_filters[column] = selected
            self.filter_archive()

    def filter_archive(self, *args):
        search = self.search.text().strip().casefold()
        for row in range(self.archive.rowCount()):
            values = [self.archive.item(row, column).text() for column in range(self.archive.columnCount())]
            visible = (not search or any(search in value.casefold() for value in values)) and all(values[column] in allowed for column, allowed in self.column_filters.items())
            self.archive.setRowHidden(row, not visible)
        for column, title in enumerate(self.archive_headers):
            self.archive.horizontalHeaderItem(column).setText(title + (" ● ▼" if column in self.column_filters else " ▼"))
        self.archive.horizontalHeader().set_active_filters(self.column_filters)

    def clear_filters(self):
        self.column_filters.clear()
        self.search.clear()
        self.filter_archive()

    def convert(self):
        try:
            if self.current is None or self.current["state"] != "complete":
                raise ValueError("Выберите завершённую калибровочную кривую")
            power = voltage_to_dbm(self.current["points"], self.convert_voltage.value())
            self.convert_result.setText(f"{power:.3f} dBm · {self.current['plan']['frequency_hz'] / 1e6:g} МГц")
        except Exception as error:
            self.convert_result.setText(str(error))

    def export_csv(self):
        if not self.current:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Точки детектора LO", "lo-detector.csv", "CSV (*.csv)")
        if path:
            try:
                with open(path, "w", encoding="utf-8-sig", newline="") as stream:
                    writer = csv.writer(stream, delimiter=";")
                    writer.writerow(["series_id", "detector", "frequency_Hz", "source", "set_dBm", "actual_dBm", "attenuation_dB", "input_dBm", "voltage_V", "std_V", "samples_V_JSON", "demo"])
                    for point in self.current["points"]:
                        writer.writerow([self.current["id"], self.current["sample"], self.current["plan"]["frequency_hz"], self.current["source_kind"],
                            point["set_dbm"], point["actual_dbm"], self.current["plan"]["input_attenuation_db"], point["input_dbm"],
                            point["voltage_v"], point["std_v"], json.dumps(point["readings_v"]), self.current["demo"]])
            except Exception as error:
                self.status.setText(str(error))

    def export_png(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "График детектора LO", "lo-detector.png", "PNG (*.png)")
        if path:
            try:
                exporter = pg.exporters.ImageExporter(self.plot.plotItem)
                exporter.parameters()["width"] = 2400
                exporter.export(path)
            except Exception as error:
                self.status.setText(str(error))

    def export_json(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Калибровки детекторов LO", "lo-detectors.json", "JSON (*.json)")
        if path:
            try:
                Path(path).write_text(json.dumps(dict(format="lo-detector-1", runs=self.store().load()), ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as error:
                self.status.setText(str(error))

    def import_json(self):
        if self.worker or self.unsafe_outputs:
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Импорт калибровок", "", "JSON (*.json)")
        if path:
            try:
                payload = json.loads(Path(path).read_text(encoding="utf-8"))
                if payload.get("format") != "lo-detector-1":
                    raise ValueError("Неизвестный формат калибровки")
                store = self.store()
                store.save_many(payload["runs"])
                self.reload()
            except Exception as error:
                self.status.setText(str(error))

    def recover(self):
        if self.worker or self.unsafe_outputs:
            return
        try:
            store = self.store()
            for path in (self.root / "data" / "detector-pending").glob("*.json"):
                payload = json.loads(path.read_text(encoding="utf-8"))
                if Path(payload["database"]).resolve() == store.path:
                    store.save(payload["run"])
                    path.unlink()
            self.reload()
        except Exception as error:
            self.status.setText(str(error))
