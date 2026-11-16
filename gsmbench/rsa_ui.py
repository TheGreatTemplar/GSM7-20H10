"""Spectrum acquisition and mixer measurements with manually set RF/LO sources."""

import csv
import json
import math
from pathlib import Path
import threading
import uuid

from PySide6 import QtCore, QtWidgets
import pyqtgraph as pg

from .mixer import compression_1db, difference_if
from .rf_storage import SpectrumStore
from .rf_charts import ArchiveCharts
from .two_tone_ui import TwoTonePanel
from .rf_methods import ISOLATION_PORTS, METHOD_CHOICES, injection_loss, injection_port, target_frequency
from .rf_sequence import active_channels, matrix_axes, plan_sequence, sequence_count
from .rsa import AcquisitionCancelled, AnalyzerConfig, Rsa6265, demo_spectrum, frequencies, mixer_result
from .smu import Smu200a, SourcePlan, verified_off


class RsaWorker(QtCore.QThread):
    log = QtCore.Signal(str)
    status = QtCore.Signal(str)
    spectrum = QtCore.Signal(object)
    failed = QtCore.Signal(str)
    generator_state = QtCore.Signal(object)

    def __init__(self, host, demo, operation, config, context, count, interval, parent, source_settings=None):
        super().__init__(parent)
        self.host, self.demo, self.operation = host, demo, operation
        self.config, self.context = config, context
        self.count, self.interval = count, interval
        self.stop = threading.Event()
        self.source_settings = source_settings

    def notify_generator(self, state, known=True):
        self.generator_state.emit(dict(known=known, state=state, demo=self.demo,
            host=self.source_settings["host"], port=self.source_settings["port"]))

    def simulated_state(self, plan=None, enabled=()):
        return dict(identity="SIMULATOR,SMU200A", options="DEMO", channels={str(channel):
            dict(output=int(channel in enabled), frequency_hz=frequency, level_dbm=level)
            for channel, (frequency, level) in (plan.channels().items() if plan else
                {1: (1e6, -30), 2: (1e6, -30)}.items())})

    def run(self):
        driver = None
        smu = None
        owns_outputs = False
        keep_on = False
        last_plan = None
        source_operation = self.operation.startswith("smu_")
        automatic = self.source_settings is not None and not source_operation
        try:
            plans = None
            if automatic:
                plans = plan_sequence(self.config, self.context, self.source_settings, self.count)
                self.count = len(plans)
            if self.source_settings and not self.demo:
                smu = Smu200a(self.source_settings["host"], self.source_settings["port"], self.log.emit)
                if self.operation != "smu_identify":
                    owns_outputs = True
            if source_operation:
                if self.operation == "smu_identify":
                    self.notify_generator(smu.state() if smu else self.simulated_state())
                    self.status.emit(smu.identity if smu else "ДЕМО SMU200A")
                    return
                if self.operation == "smu_off":
                    try:
                        self.notify_generator(verified_off(smu) if smu else self.simulated_state())
                    except Exception:
                        self.notify_generator({}, known=False)
                        raise
                    finally:
                        owns_outputs = False
                    self.status.emit("SMU: оба выхода OFF подтверждены")
                    return
                settings = self.source_settings
                source_rf = self.context["if_hz"] if injection_port(self.context["method"]) == "IF" else self.context["rf_hz"]
                plan = SourcePlan(source_rf, self.context["lo_hz"], settings["rf_dbm"],
                                  settings["lo_dbm"], settings["rf_channel"], settings["lo_channel"])
                plan.validate()
                state = smu.prepare(plan) if smu else self.simulated_state(plan)
                if self.operation == "smu_on":
                    if self.stop.is_set():
                        raise AcquisitionCancelled()
                    enabled = active_channels(self.context, plan)
                    state = smu.enable(enabled, self.stop) if smu else self.simulated_state(plan, enabled)
                    keep_on = True
                self.notify_generator(state)
                self.status.emit("SMU: выходы включены по схеме измерения" if keep_on else "SMU: CW настроен, выходы OFF")
                return
            if automatic and smu:
                smu.all_off()
            if not self.demo:
                driver = Rsa6265(self.host, self.log.emit)
            self.status.emit("ДЕМО RSA6265" if self.demo else driver.identity)
            if self.operation == "identify":
                return
            if not automatic and self.operation in ("apply", "single", "series") and driver:
                actual = driver.configure(self.config)
                self.log.emit("Фактические настройки: " + json.dumps(actual, ensure_ascii=False))
            if self.operation == "apply":
                return
            series_id = str(uuid.uuid4())
            compression_points = []
            compression_groups = {}
            current_config = None
            configured_settings = None
            for index in range(self.count):
                if self.stop.is_set():
                    break
                config, context = self.config, dict(self.context)
                generator = None
                if automatic:
                    config, context, plan = plans[index]
                    last_plan = plan
                    if smu:
                        generator = smu.prepare(plan) if index == 0 else smu.setpoint(plan)
                        if index == 0:
                            for _, _, planned in plans:
                                smu.validate_plan(planned)
                            generator = smu.enable(active_channels(context, plan), self.stop)
                    else:
                        generator = self.simulated_state(plan, active_channels(context, plan))
                    self.notify_generator(generator)
                    context["rf_actual_dbm"] = generator["channels"][str(plan.rf_channel)]["level_dbm"]
                    context["lo_actual_dbm"] = generator["channels"][str(plan.lo_channel)]["level_dbm"]
                    context["pin_dbm"] = (context["lo_actual_dbm"] if injection_port(context["method"]) == "LO"
                                          else context["rf_actual_dbm"]) - injection_loss(context, context)
                    if self.stop.wait(self.source_settings["settling_s"]):
                        raise AcquisitionCancelled()
                    if driver and config != current_config:
                        configured_settings = driver.configure(config)
                        current_config = config
                spectrum = (demo_spectrum(config) if self.demo else
                            driver.read_current() if self.operation == "read" else driver.acquire(self.stop))
                if automatic and configured_settings and spectrum["settings"] != configured_settings:
                    raise ValueError("Настройки RSA изменились после настройки серии; спектр не принят")
                if automatic and smu and smu.state() != generator:
                    raise ValueError("Параметры/выходы SMU изменились во время развёртки; спектр не принят")
                if automatic and self.demo:
                    pin = context["rf_set_dbm"] - context["rf_loss_db"]
                    loss = 7 + max(0, pin + 15) * 0.2 + abs(context["lo_set_dbm"] - 13) * 0.05
                    output = (context["pin_dbm"] - 45 if injection_port(context["method"]) == "LO" else
                              context["pin_dbm"] - 55 if context["method"] in ISOLATION_PORTS else pin - loss)
                    shift = output - context["output_loss_db"] + 27
                    spectrum["power_dbm"] = [max(-105, power + shift) for power in spectrum["power_dbm"]]
                context.update(series_id=series_id, series_index=index)
                spectrum["context"] = context
                if generator:
                    spectrum["generator"] = generator
                spectrum["result"] = mixer_result(spectrum)
                if automatic and context["sweep_kind"] in ("rf_power", "matrix") and context["method"] == "conversion":
                    result = spectrum["result"]
                    if result["status"] == "preliminary":
                        compression_points.append((context["pin_dbm"], result["marker"]["power_dbm"] + context["output_loss_db"]))
                        key = (context["rf_hz"], context["lo_actual_dbm"] - context["lo_loss_db"])
                        compression_groups.setdefault(key, []).append(compression_points[-1])
                    if index + 1 == self.count:
                        try:
                            if len(compression_points) != self.count:
                                raise ValueError("Часть точек не имеет достоверного расчёта")
                            if context["sweep_kind"] == "matrix":
                                slices = []
                                for (rf, lo), points in compression_groups.items():
                                    try:
                                        value = compression_1db(sorted(points))
                                        slices.append(dict(rf_hz=rf, lo_dbm=lo, status="preliminary" if value else "not_reached", result=value))
                                    except ValueError as error:
                                        slices.append(dict(rf_hz=rf, lo_dbm=lo, status="not_calculated", reason=str(error)))
                                spectrum["series_summary"] = dict(kind="matrix_p1db", status="preliminary", slices=slices)
                            else:
                                value = compression_1db(compression_points)
                                spectrum["series_summary"] = dict(kind="p1db", status="preliminary" if value else "not_reached", result=value)
                        except ValueError as error:
                            spectrum["series_summary"] = dict(kind="p1db", status="not_calculated", reason=str(error))
                self.spectrum.emit(spectrum)
                self.status.emit(f"Спектр {index + 1} / {self.count}")
                if index + 1 < self.count and self.stop.wait(self.interval):
                    break
        except (AcquisitionCancelled, InterruptedError):
            self.status.emit("Остановлено; отключаю управляемые выходы SMU" if owns_outputs else "RSA остановлен; внешние источники под ручным управлением")
        except Exception as error:
            self.failed.emit(str(error))
        finally:
            if owns_outputs and not keep_on:
                try:
                    self.notify_generator(verified_off(smu))
                except Exception as error:
                    self.notify_generator({}, known=False)
                    self.failed.emit("RF OFF не подтверждён: " + str(error))
            elif automatic and self.demo:
                self.notify_generator(self.simulated_state(last_plan))
            if smu:
                try:
                    smu.close()
                except Exception as error:
                    self.failed.emit("Закрытие связи SMU: " + str(error))
            if driver:
                try:
                    driver.close()
                except Exception as error:
                    self.failed.emit("Закрытие связи RSA: " + str(error))


class RsaWindow(QtWidgets.QWidget):
    settings_changed = QtCore.Signal(object)

    def __init__(self, parent, root, settings):
        super().__init__(parent)
        self.root = Path(root)
        self.worker = None
        self.current = None
        self.spectra = []
        self.last_error = None
        self.saved_count = 0
        self.generator_active = False
        self.generator_endpoint = None
        self.stop_pending = False
        self.cleanup_error = None
        self.showing_series = False
        self.column_filters = {}
        self.fields = {}
        self.build()
        for key, widget in self.fields.items():
            if key not in settings:
                continue
            value = settings[key]
            if isinstance(widget, QtWidgets.QCheckBox):
                widget.setChecked(bool(value))
            elif isinstance(widget, QtWidgets.QComboBox):
                index = widget.findData(value)
                if index >= 0:
                    widget.setCurrentIndex(index)
            elif isinstance(widget, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
                widget.setValue(value)
            else:
                widget.setText(str(value))
        for widget in self.fields.values():
            signal = (widget.toggled if isinstance(widget, QtWidgets.QCheckBox) else
                      widget.currentIndexChanged if isinstance(widget, QtWidgets.QComboBox) else
                      widget.valueChanged if isinstance(widget, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)) else
                      widget.textChanged)
            signal.connect(self.changed)
        self.sweep_kind.currentIndexChanged.connect(self.sweep_defaults)
        self.update_lo()
        self.update_source_controls()
        tab_index = settings.get("setup_tab", 0)
        if type(tab_index) is int and 0 <= tab_index < self.setup_tabs.count():
            self.setup_tabs.setCurrentIndex(tab_index)
        self.setup_tabs.currentChanged.connect(lambda *args: self.settings_changed.emit(self.settings()))
        self.column_filters = {int(key): set(values) for key, values in settings.get("archive_filters", {}).items()
            if str(key).isdigit() and 0 <= int(key) < self.table.columnCount() and isinstance(values, list)}
        self.search.setText(str(settings.get("archive_search", "")))
        self.charts.restore(settings.get("chart", {}))
        self.two_tone.restore(settings.get("two_tone", {}))
        self.views.setCurrentIndex(settings.get("view_tab", 0) if settings.get("view_tab", 0) in (0, 1, 2) else 0)
        self.reload()

    def field(self, key, label, widget, form):
        self.fields[key] = widget
        form.addRow(label, widget)
        setattr(self, key, widget)
        return widget

    def spin(self, value, minimum, maximum, decimals=3):
        widget = QtWidgets.QDoubleSpinBox()
        widget.setDecimals(decimals)
        widget.setRange(minimum, maximum)
        widget.setValue(value)
        return widget

    def combo(self, choices):
        widget = QtWidgets.QComboBox()
        for label, data in choices:
            widget.addItem(label, data)
        return widget

    def button(self, label, slot, layout):
        widget = QtWidgets.QPushButton(label)
        widget.clicked.connect(slot)
        layout.addWidget(widget)
        return widget

    def build(self):
        layout = QtWidgets.QVBoxLayout(self)
        heading = QtWidgets.QLabel("СМЕСИТЕЛИ · RF измерения")
        heading.setStyleSheet("font-size:22px; font-weight:700")
        layout.addWidget(heading)
        self.notice = QtWidgets.QLabel()
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self.smu_status = QtWidgets.QLabel("SMU: состояние не опрошено · подключение только по кнопке")
        self.smu_status.setWordWrap(True)
        layout.addWidget(self.smu_status)
        body = QtWidgets.QHBoxLayout()
        layout.addLayout(body, 1)
        self.form_panel = QtWidgets.QWidget()
        self.form_panel.setMinimumWidth(350)
        self.form_panel.setMaximumWidth(430)
        panel_layout = QtWidgets.QVBoxLayout(self.form_panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        self.setup_tabs = QtWidgets.QTabWidget()
        panel_layout.addWidget(self.setup_tabs)
        pages = []
        for label in ("Измерения", "Генератор", "Анализатор"):
            scroll = QtWidgets.QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            page = QtWidgets.QWidget()
            pages.append(QtWidgets.QVBoxLayout(page))
            scroll.setWidget(page)
            self.setup_tabs.addTab(scroll, label)
        left, generator_layout, analyzer_layout = pages
        body.addWidget(self.form_panel)
        connection = QtWidgets.QGroupBox("Связь и отдельная БД смесителей")
        form = QtWidgets.QFormLayout(connection)
        self.field("demo", "Демонстрация", QtWidgets.QCheckBox("Синтетические спектры"), form)
        self.field("database", "Реальная БД", QtWidgets.QLineEdit(str(self.root / "data" / "mixers.db")), form)
        buttons = QtWidgets.QHBoxLayout()
        form.addRow(buttons)
        self.button("Выбрать БД…", self.choose_database, buttons)
        self.button("Создать БД…", self.create_database, buttons)
        left.addWidget(connection)
        recipe = QtWidgets.QGroupBox("Смеситель и внешние сигналы")
        form = QtWidgets.QFormLayout(recipe)
        self.field("sample", "Смеситель", QtWidgets.QLineEdit("MX001"), form)
        self.field("batch", "Партия", QtWidgets.QLineEdit(), form)
        self.field("model", "Тип / модель", QtWidgets.QLineEdit(), form)
        self.field("notes", "Примечание / диоды", QtWidgets.QLineEdit(), form)
        self.field("rf_mhz", "RF, МГц", self.spin(14, 0.1, 60, 6), form)
        self.field("if_hz", "ПЧ", self.combo([("11,0592 МГц", 11059200), ("8,192 МГц", 8192000)]), form)
        self.field("lo_side", "Гетеродин", self.combo([("Выше RF", "upper"), ("Ниже RF", "lower")]), form)
        self.lo_label = QtWidgets.QLabel()
        form.addRow("LO, расчёт", self.lo_label)
        self.field("method", "Измерение", self.combo(METHOD_CHOICES), form)
        self.field("pin_dbm", "Уровень на входе, dBm", self.spin(-20, -140, 30), form)
        self.field("pin_known", "Уровень", QtWidgets.QCheckBox("Известен на порту смесителя"), form)
        self.field("output_loss_db", "Ослабление до RSA, дБ", self.spin(0, 0, 100), form)
        self.field("path_known", "Выходной тракт", QtWidgets.QCheckBox("Поправка проверена на частоте маркера"), form)
        self.field("marker_window_hz", "Поиск пика ±, Гц", self.spin(3000, 1, 1e8, 0), form)
        self.wiring = QtWidgets.QLabel()
        self.wiring.setWordWrap(True)
        form.addRow(self.wiring)
        left.addWidget(recipe)
        generator = QtWidgets.QGroupBox("R&S SMU200A · RF и гетеродин")
        form = QtWidgets.QFormLayout(generator)
        self.field("smu_host", "IP SMU", QtWidgets.QLineEdit("169.254.234.52"), form)
        port = QtWidgets.QSpinBox()
        port.setRange(1, 65535)
        port.setValue(5025)
        self.field("smu_port", "TCP-порт", port, form)
        channels = [("A / выход 1", 1), ("B / выход 2", 2)]
        self.field("rf_channel", "RF / ввод IF", self.combo(channels), form).setCurrentIndex(1)
        self.field("lo_channel", "LO", self.combo(channels), form)
        self.field("rf_dbm", "RF / IF на выходе, dBm", self.spin(-20, -145, 20), form)
        self.field("lo_dbm", "LO на выходе, dBm", self.spin(13, -145, 20), form)
        self.field("rf_loss_db", "Потери RF-тракта, дБ", self.spin(0, 0, 100), form)
        self.field("lo_loss_db", "Потери LO-тракта, дБ", self.spin(0, 0, 100), form)
        self.field("if_loss_db", "Потери ввода IF, дБ", self.spin(0, 0, 100), form)
        self.field("source_path_known", "Поправки входов", QtWidgets.QCheckBox("Потери RF/LO проверены"), form)
        self.field("settling_s", "Установление, с", self.spin(0.2, 0, 10), form)
        self.button("Связь / состояние SMU", lambda: self.launch("smu_identify"), form)
        self.button("Применить CW · выходы OFF", lambda: self.launch("smu_apply"), form)
        self.button("Включить по схеме измерения", lambda: self.launch("smu_on"), form)
        self.button("Отключить оба выхода SMU", lambda: self.launch("smu_off"), form)
        generator_layout.addWidget(generator)
        second_generator = QtWidgets.QGroupBox("АКИП-3208 / SSG3000X · LO двухтонового теста")
        form = QtWidgets.QFormLayout(second_generator)
        self.field("akip_host", "IP АКИП", QtWidgets.QLineEdit("172.16.236.190"), form)
        akip_port = QtWidgets.QSpinBox()
        akip_port.setRange(1, 65535)
        akip_port.setValue(5025)
        self.field("akip_port", "TCP-порт", akip_port, form)
        self.button("Связь / состояние АКИП", self.identify_akip, form)
        generator_layout.addWidget(second_generator)
        generator_layout.addStretch()
        analyzer = QtWidgets.QGroupBox("RSA · развёртка SA / dBm")
        form = QtWidgets.QFormLayout(analyzer)
        self.field("host", "IP RSA", QtWidgets.QLineEdit("172.16.236.90"), form)
        self.button("Проверить связь RSA", lambda: self.launch("identify"), form)
        self.field("center_mhz", "Центр, МГц", self.spin(11.0592, 0.000001, 26500, 6), form)
        self.field("span_khz", "Обзор, кГц", self.spin(200, 0.001, 26500000), form)
        bandwidths = [(f"{value:g} Гц", value) for value in
                      (1, 3, 10, 30, 100, 300, 1000, 3000, 10000, 30000, 100000, 300000, 1000000, 3000000, 10000000)]
        self.field("rbw_hz", "RBW", self.combo(bandwidths), form).setCurrentIndex(6)
        self.field("vbw_hz", "VBW", self.combo(bandwidths), form).setCurrentIndex(6)
        attenuation = QtWidgets.QSpinBox()
        attenuation.setRange(0, 50)
        attenuation.setValue(10)
        self.field("attenuation_db", "Аттенюатор, дБ", attenuation, form)
        self.field("reference_dbm", "Опорный уровень, dBm", self.spin(0, -170, 30), form)
        count = QtWidgets.QSpinBox()
        count.setRange(101, 10001)
        count.setValue(801)
        self.field("points", "Точек", count, form)
        self.field("detector", "Детектор", self.combo([("Положительный пик", "POS"),
            ("Нормальный", "NORM"), ("RMS", "RMS"), ("Выборка", "SAMP")]), form)
        self.button("Обзор вокруг ожидаемого тона", self.center_on_tone, form)
        self.button("Применить настройки RSA", lambda: self.launch("apply"), form)
        analyzer_layout.addWidget(analyzer)
        analyzer_layout.addStretch()
        series = QtWidgets.QGroupBox("Запись спектров")
        form = QtWidgets.QFormLayout(series)
        self.field("use_smu", "Автоматизация", QtWidgets.QCheckBox("Управлять источниками при измерении"), form)
        count = QtWidgets.QSpinBox()
        count.setRange(1, 1000)
        count.setValue(10)
        self.field("series_count", "Число спектров", count, form)
        self.field("interval_s", "Пауза между, с", self.spin(1, 0, 3600), form)
        self.field("sweep_kind", "Тип серии", self.combo([("Повторять спектр", "fixed"),
            ("RF по частоте · МГц", "rf_frequency"), ("RF по уровню · dBm / P1dB", "rf_power"),
            ("LO по уровню · dBm", "lo_power"), ("Комплексная проверка КВ", "matrix")]), form)
        self.field("sweep_start", "Начало · МГц / dBm", self.spin(3, -145, 60, 6), form)
        self.field("sweep_stop", "Конец · МГц / dBm", self.spin(30, -145, 60, 6), form)
        self.field("matrix_rf_mhz", "КВ · частоты RF, МГц", QtWidgets.QLineEdit("1.85; 3.65; 5.35; 7.1; 10.12; 14.15; 18.1; 21.2; 24.93; 28.5"), form)
        self.field("matrix_rf_dbm", "Список RF, dBm", QtWidgets.QLineEdit("-40; -30; -20; -10; 0"), form)
        self.field("matrix_lo_dbm", "Список LO, dBm", QtWidgets.QLineEdit("0; 3; 7; 10; 13; 17; 20"), form)
        self.matrix_count = QtWidgets.QLabel()
        self.matrix_count.setWordWrap(True)
        form.addRow(self.matrix_count)
        left.addWidget(series)
        left.addStretch()
        for form_layout in self.form_panel.findChildren(QtWidgets.QFormLayout):
            form_layout.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
            form_layout.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.views = QtWidgets.QTabWidget()
        body.addWidget(self.views, 1)
        acquisition_page = QtWidgets.QWidget()
        right = QtWidgets.QVBoxLayout(acquisition_page)
        self.views.addTab(acquisition_page, "Измерение и архив")
        tools = QtWidgets.QHBoxLayout()
        right.addLayout(tools)
        self.button("Сбросить масштаб", lambda: self.plot.enableAutoRange(), tools)
        export = self.button("Экспорт CSV", lambda: None, tools)
        export_menu = QtWidgets.QMenu(export)
        export_menu.addAction("Текущий спектр…", self.export_csv)
        export_menu.addAction("Сводка серии…", self.export_series_csv)
        export.setMenu(export_menu)
        self.button("Кривая серии", self.show_series_curve, tools)
        acquisition_row = QtWidgets.QHBoxLayout()
        right.addLayout(acquisition_row)
        self.operation_buttons = []
        for label, operation, tooltip in (
            ("Считать трассу", "read", "Только чтение текущей Trace1; параметры RSA не меняются"),
            ("Одиночное", "single", "Применить настройки слева и выполнить новую одиночную развёртку"),
            ("Серия", "series", "Применить настройки слева и выполнить серию новых развёрток")):
            button = self.button(label, lambda checked=False, operation=operation: self.launch(operation), acquisition_row)
            button.setToolTip(tooltip)
            self.operation_buttons.append(button)
        self.plot = pg.PlotWidget(title="Спектр · RSA6265")
        self.label_axis("bottom", "Частота", "Hz", True)
        self.label_axis("left", "Уровень", "dBm", False)
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        right.addWidget(self.plot, 3)
        self.marker_text = QtWidgets.QLabel("Наведите курсор на спектр: частота, уровень и разность относительно пика")
        self.marker_text.setWordWrap(True)
        right.addWidget(self.marker_text)
        self.plot.scene().sigMouseMoved.connect(self.move_marker)
        self.result = QtWidgets.QLabel()
        self.result.setWordWrap(True)
        right.addWidget(self.result)
        tabs = QtWidgets.QTabWidget()
        right.addWidget(tabs, 2)
        archive = QtWidgets.QWidget()
        archive_layout = QtWidgets.QVBoxLayout(archive)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Поиск в архиве смесителей…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.filter_archive)
        archive_layout.addWidget(self.search)
        filter_row = QtWidgets.QHBoxLayout()
        archive_layout.addLayout(filter_row)
        self.filter_count = QtWidgets.QLabel()
        filter_row.addWidget(self.filter_count, 1)
        self.button("Сбросить фильтры", self.clear_archive_filters, filter_row)
        self.button("Графики по архиву", self.show_charts, filter_row)
        self.archive_headers = ["Смеситель", "Партия", "Тип / модель", "Дата UTC", "Режим", "RF, МГц", "LO, МГц",
            "ПЧ, МГц", "RF / IF, dBm", "LO, dBm", "Результат, дБ", "Статус", "Примечание"]
        self.table = QtWidgets.QTableWidget(0, len(self.archive_headers))
        self.table.setHorizontalHeaderLabels(self.archive_headers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().sectionClicked.connect(self.show_archive_filter)
        self.table.itemSelectionChanged.connect(self.show_selected)
        archive_layout.addWidget(self.table)
        row = QtWidgets.QHBoxLayout()
        archive_layout.addLayout(row)
        self.button("Обновить", self.reload, row)
        self.button("Экспорт JSON…", self.export_json, row)
        self.button("Импорт JSON…", self.import_json, row)
        self.button("Восстановить", self.recover, row)
        tabs.addTab(archive, "Архив спектров")
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        tabs.addTab(self.log, "Журнал RSA")
        self.charts = ArchiveCharts(self.chart_spectra, self)
        self.charts.settings_changed.connect(lambda: self.settings_changed.emit(self.settings()))
        self.views.addTab(self.charts, "Графики и отчёты")
        self.two_tone = TwoTonePanel(self)
        self.two_tone.settings_changed.connect(lambda: self.settings_changed.emit(self.settings()))
        self.two_tone.busy_changed.connect(self.two_tone_busy)
        self.views.addTab(self.two_tone, "Два тона · смеситель / PA")
        self.views.currentChanged.connect(lambda *args: self.settings_changed.emit(self.settings()))
        bottom = QtWidgets.QHBoxLayout()
        layout.addLayout(bottom)
        self.status = QtWidgets.QLabel("Готов · подключение только по кнопке")
        self.status.setWordWrap(True)
        bottom.addWidget(self.status, 1)
        self.stop_button = self.button("СТОП / RF OFF", self.stop, bottom)
        self.stop_button.setEnabled(False)

    def settings(self):
        return dict(setup_tab=self.setup_tabs.currentIndex(), view_tab=self.views.currentIndex(),
                    archive_search=self.search.text(), archive_filters={str(key): sorted(values) for key, values in self.column_filters.items()},
                    chart=self.charts.settings(), two_tone=self.two_tone.settings(), **{key: (widget.isChecked() if isinstance(widget, QtWidgets.QCheckBox) else
                      widget.currentData() if isinstance(widget, QtWidgets.QComboBox) else
                      widget.value() if isinstance(widget, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)) else
                      widget.text()) for key, widget in self.fields.items()})

    def label_axis(self, name, text, units, auto_prefix):
        axis = self.plot.getAxis(name)
        axis.setLabel(text=text, units=units)
        # pyqtgraph 0.14 updates the SI factor once even when disabling it.
        # Exclude all scaling ranges before that update to discard an old MHz factor.
        if hasattr(axis, "setSIPrefixEnableRanges"):
            axis.setSIPrefixEnableRanges(None if auto_prefix else ())
        axis.enableAutoSIPrefix(auto_prefix)

    def changed(self, *args):
        self.update_lo()
        self.update_source_controls()
        self.database.setToolTip(self.database.text())
        self.settings_changed.emit(self.settings())
        if self.sender() in (self.demo, self.database):
            self.current = None
            self.plot.clear()
            self.result.clear()
            self.marker_text.clear()
            self.reload()

    def update_source_controls(self):
        automatic = self.use_smu.isChecked()
        self.notice.setText("Авто SMU: одиночное/серия включают источники по схеме измерения и отключают их при завершении" if automatic else
                            "Ручные источники: одиночное/серия управляют только RSA; SMU доступен отдельными кнопками")
        self.pin_dbm.setEnabled(not automatic)
        self.pin_known.setEnabled(not automatic)
        self.update_wiring()
        for widget in (self.demo, self.smu_host, self.smu_port):
            widget.setEnabled(not self.generator_active)
        matrix = self.sweep_kind.currentData() == "matrix"
        self.series_count.setEnabled(not matrix)
        self.sweep_start.setEnabled(not matrix)
        self.sweep_stop.setEnabled(not matrix)
        for widget in (self.matrix_rf_mhz, self.matrix_rf_dbm, self.matrix_lo_dbm):
            widget.setEnabled(matrix)
            widget.parentWidget().layout().setRowVisible(widget, matrix)
        self.matrix_count.setVisible(matrix)
        self.sweep_start.parentWidget().layout().setRowVisible(self.sweep_start, self.sweep_kind.currentData() not in ("fixed", "matrix"))
        self.sweep_stop.parentWidget().layout().setRowVisible(self.sweep_stop, self.sweep_kind.currentData() not in ("fixed", "matrix"))
        try:
            axes = matrix_axes(self.source_settings())
            self.matrix_count.setText(f"Комплексная: {len(axes[0])} частот × {len(axes[1])} RF × {len(axes[2])} LO = "
                                      f"{math.prod(len(axis) for axis in axes)} спектров на выбранной ПЧ")
        except ValueError as error:
            self.matrix_count.setText("Списки комплексной проверки: " + str(error))

    def update_wiring(self):
        method = self.method.currentData()
        channel = lambda value: "A / 1" if value == 1 else "B / 2"
        if method in ISOLATION_PORTS:
            source, output = ISOLATION_PORTS[method]
            spare = next(port for port in ("RF", "LO", "IF") if port not in (source, output))
            selected_channel = self.lo_channel.currentData() if source == "LO" else self.rf_channel.currentData()
            self.wiring.setText(f"Схема {source} → {output}: SMU {channel(selected_channel)} → {source}; "
                f"порт {output} → RSA; порт {spare} → нагрузка 50 Ом. "
                f"Включается только вводимый тон {source}; RSA измеряет утечку на частоте этого тона. "
                "Перед перестановкой кабелей отключите оба выхода SMU. Для ввода IF используется уровень RF / IF.")
        else:
            self.wiring.setText(f"SMU {channel(self.lo_channel.currentData())} → LO; "
                f"SMU {channel(self.rf_channel.currentData())} → RF; IF → RSA. "
                "Для потерь преобразования включаются RF и LO, измеряется разностная ПЧ.")
        self.if_loss_db.setEnabled(injection_port(method) == "IF")

    def source_settings(self):
        data = self.settings()
        return dict(host=data["smu_host"].strip(), port=data["smu_port"],
                    **{key: data[key] for key in ("rf_channel", "lo_channel", "rf_dbm", "lo_dbm", "rf_loss_db",
                       "lo_loss_db", "if_loss_db", "source_path_known", "settling_s", "sweep_kind", "sweep_start", "sweep_stop",
                       "matrix_rf_mhz", "matrix_rf_dbm", "matrix_lo_dbm")})

    def sweep_defaults(self):
        bounds = {"rf_frequency": (3, 30), "rf_power": (-40, 0), "lo_power": (0, 20)}
        if self.sweep_kind.currentData() in bounds:
            start, stop = bounds[self.sweep_kind.currentData()]
            self.sweep_start.setValue(start)
            self.sweep_stop.setValue(stop)
        elif self.sweep_kind.currentData() == "matrix":
            self.use_smu.setChecked(True)
            self.method.setCurrentIndex(self.method.findData("conversion"))

    def generator_updated(self, event):
        self.generator_endpoint = dict(host=event["host"], port=event["port"], demo=event["demo"])
        if not event["known"]:
            self.generator_active = True
            self.smu_status.setText("SMU: отключение RF не подтверждено. Проверьте RF ON на приборе и повторите OFF.")
        else:
            state = event["state"]
            self.generator_active = any(channel["output"] for channel in state["channels"].values())
            self.smu_status.setText(("ДЕМО · " if event["demo"] else "SMU · ") + " · ".join(
                f"{'A' if key == '1' else 'B'}: {'ON' if channel['output'] else 'OFF'}, "
                f"{channel['frequency_hz'] / 1e6:.6f} МГц, {channel['level_dbm']:.2f} dBm"
                for key, channel in sorted(state["channels"].items())))
        self.update_source_controls()
        self.stop_button.setEnabled(self.worker is not None or self.generator_active)
        self.smu_status.setStyleSheet("color: " + ("#f4bd61" if self.generator_active else "#45d7d0"))

    def update_lo(self):
        rf, intermediate = self.rf_mhz.value() * 1e6, self.if_hz.currentData()
        lo = rf + intermediate if self.lo_side.currentData() == "upper" else rf - intermediate
        self.lo_label.setText(f"{lo / 1e6:.6f} МГц" if lo > 0 else "Невозможно: RF ниже ПЧ")

    def context(self):
        data = self.settings()
        rf, intermediate = data["rf_mhz"] * 1e6, data["if_hz"]
        lo = rf + intermediate if data["lo_side"] == "upper" else rf - intermediate
        if lo <= 0 or not math.isclose(difference_if(rf, lo), intermediate, rel_tol=0, abs_tol=0.001):
            raise ValueError("Неверный частотный план RF/LO/IF")
        if not data["sample"].strip():
            raise ValueError("Укажите номер смесителя")
        keys = ("sample", "batch", "model", "notes", "method", "pin_dbm", "pin_known", "output_loss_db", "path_known", "marker_window_hz")
        return dict(rf_hz=rf, lo_hz=lo, if_hz=intermediate, sources="manual",
                    **{key: data[key] for key in keys})

    def config(self):
        data = self.settings()
        return AnalyzerConfig(center_hz=data["center_mhz"] * 1e6, span_hz=data["span_khz"] * 1000,
            **{key: data[key] for key in ("rbw_hz", "vbw_hz", "attenuation_db", "reference_dbm", "points", "detector")})

    def target(self, context):
        return target_frequency(context)

    def center_on_tone(self):
        try:
            self.center_mhz.setValue(self.target(self.context()) / 1e6)
        except Exception as error:
            self.error(str(error))

    def store(self):
        if self.demo.isChecked():
            return SpectrumStore(self.root / "data" / "mixers-demo.db", True)
        if not self.database.text().strip():
            raise ValueError("Укажите отдельную БД смесителей")
        return SpectrumStore(self.database.text(), False)

    def launch(self, operation):
        if self.worker or self.two_tone.worker or self.two_tone.unsafe_outputs:
            return
        try:
            source_job = operation.startswith("smu_")
            source_settings = self.source_settings() if source_job or (
                self.use_smu.isChecked() and operation in ("single", "series")) else None
            job_demo = self.demo.isChecked()
            if operation == "smu_off" and self.generator_active and self.generator_endpoint:
                source_settings.update(host=self.generator_endpoint["host"], port=self.generator_endpoint["port"])
                job_demo = self.generator_endpoint["demo"]
            if source_settings and operation == "single":
                source_settings["sweep_kind"] = "fixed"
            config = self.config()
            if not source_job and (operation in ("apply", "single", "series") or self.demo.isChecked()):
                config.validate()
            context = self.context() if operation not in ("identify", "apply", "smu_identify", "smu_off") else {}
            if not source_job and operation not in ("identify", "apply"):
                if operation == "series" and self.sweep_kind.currentData() != "fixed" and not source_settings:
                    raise ValueError("Для перестройки частоты/уровня включите управление SMU")
                self.active_store = self.store()
                self.active_store.initialize()
            self.settings_changed.emit(self.settings())
            self.last_error = None
            self.saved_count = 0
            self.form_panel.setEnabled(False)
            for button in self.operation_buttons:
                button.setEnabled(False)
            self.stop_button.setEnabled(True)
            self.status.setText("RSA: выполняется " + operation)
            count = sequence_count(source_settings, self.series_count.value()) if source_settings and operation == "series" else (
                self.series_count.value() if operation == "series" else 1)
            self.worker = RsaWorker(self.host.text().strip(), job_demo, operation, config, context,
                count, self.interval_s.value(), self, source_settings)
            self.worker.log.connect(self.log.appendPlainText)
            self.worker.status.connect(self.status.setText)
            self.worker.failed.connect(self.error)
            self.worker.generator_state.connect(self.generator_updated)
            self.worker.spectrum.connect(self.received)
            self.worker.finished.connect(self.worker_finished)
            self.worker.start()
        except Exception as error:
            if self.worker is None:
                self.form_panel.setEnabled(True)
                for button in self.operation_buttons:
                    button.setEnabled(True)
                self.stop_button.setEnabled(self.generator_active)
                self.update_source_controls()
            self.error(str(error))

    def received(self, spectrum):
        recovery = self.root / "data" / "rf-pending" / (spectrum["id"] + ".json")
        self.display(spectrum)
        try:
            recovery.parent.mkdir(parents=True, exist_ok=True)
            recovery.write_text(json.dumps(dict(database=str(self.active_store.path), spectrum=spectrum),
                                           ensure_ascii=False, indent=4), encoding="utf-8")
            self.active_store.save_many([spectrum])
            self.saved_count += 1
            recovery.unlink()
            self.reload()
        except Exception as error:
            if self.worker:
                self.worker.stop.set()
            self.error(f"Не удалось сохранить спектр: {error}\nРезерв: {recovery}")

    def display(self, spectrum):
        self.current = spectrum
        self.showing_series = False
        self.label_axis("bottom", "Частота", "Hz", True)
        self.label_axis("left", "Уровень", "dBm", False)
        hz, power = frequencies(spectrum), spectrum["power_dbm"]
        self.frequency_axis = hz
        self.peak_power = max(power)
        self.plot.clear()
        legend = self.plot.getPlotItem().legend
        if legend:
            legend.clear()
            legend.hide()
        self.plot.plot(hz, power, pen=pg.mkPen("#45d7d0", width=1.5))
        self.cross_x = pg.InfiniteLine(angle=90, pen=pg.mkPen("#f4bd61", style=QtCore.Qt.PenStyle.DashLine))
        self.cross_y = pg.InfiniteLine(angle=0, pen=pg.mkPen("#f4bd61", style=QtCore.Qt.PenStyle.DashLine))
        for item in (self.cross_x, self.cross_y):
            self.plot.addItem(item, ignoreBounds=True)
            item.hide()
        peak = max(range(len(power)), key=power.__getitem__)
        self.marker_text.setText(f"Пик: {hz[peak] / 1e6:.6f} МГц · {power[peak]:.3f} dBm")
        self.plot.setTitle(("ДЕМО · " if spectrum["demo"] else "RSA6265 · ") + spectrum.get("context", {}).get("sample", ""))
        self.plot.enableAutoRange()
        self.analyze(spectrum)

    def analyze(self, spectrum):
        context = spectrum.get("context", {})
        result = mixer_result(spectrum)
        if result.get("kind") == "two_tone":
            self.result.setText((f"Два тона: Pср {result['output_average_w']:.4g} Вт; PEP {result['output_pep_w']:.4g} Вт. "
                + ("Поправки подтверждены." if result["calibrated"] else "Номинальные поправки, IP3 не рассчитан."))
                if result["status"] == "preliminary" else "Два тона: " + result["reason"])
            return
        self.result.setText("Спектр сохранён. Настройки и исходные отсчёты находятся в протоколе.")
        if result["status"] == "spectrum_only":
            return
        if result["status"] == "preliminary":
            marker = result["marker"]
            if result.get("kind") == "harmonic" and result.get("value_db") is None:
                self.result.setText("Гармоническая полоса ниже порога обнаружения; численный уровень искажений не определён.")
                return
            label = "Подавление гармонической полосы" if result.get("kind") == "harmonic" else (
                "Потери преобразования" if context["method"] == "conversion" else "Развязка")
            self.result.setText(f"Предварительно: {label} = {result['value_db']:.3f} дБ · "
                f"маркер {marker['frequency_hz'] / 1e6:.6f} МГц / {marker['power_dbm']:.3f} dBm. "
                "Шумовой пол, перегрузку и настройку внешних источников проверьте отдельно.")
        else:
            self.result.setText("Расчёт не выполнен: " + result["reason"])
        summary = self.summary_for(spectrum)
        if summary:
            if summary["kind"] == "matrix_p1db":
                selected = next((item for item in summary["slices"] if item["rf_hz"] == context["rf_hz"] and
                                 item["lo_dbm"] == context["lo_actual_dbm"] - context["lo_loss_db"]), None)
                self.result.setText(self.result.text() + f"\nКомплексная проверка: {len(summary['slices'])} срезов P1dB.")
                summary = selected
            if not summary:
                return
            if summary["status"] == "preliminary":
                text = (f"P1dB предварительно: вход {summary['result']['input_p1db_dbm']:.3f} dBm; "
                        f"выход {summary['result']['output_p1db_dbm']:.3f} dBm.")
            elif summary["status"] == "not_reached":
                text = "P1dB в заданном диапазоне не достигнут."
            else:
                text = "P1dB не рассчитан: " + summary["reason"]
            self.result.setText(self.result.text() + "\n" + text)

    def summary_for(self, spectrum):
        if "series_summary" in spectrum:
            return spectrum["series_summary"]
        series_id = spectrum.get("context", {}).get("series_id")
        return next((item["series_summary"] for item in self.spectra if series_id and
                     item.get("context", {}).get("series_id") == series_id and "series_summary" in item), None)

    def show_series_curve(self):
        if not self.current:
            return
        context = self.current.get("context", {})
        series_id = context.get("series_id")
        kind = context.get("sweep_kind", "fixed")
        if kind == "two_tone_power":
            self.views.setCurrentWidget(self.two_tone)
            self.two_tone.load_archive()
            return
        if not series_id or kind == "fixed":
            self.error("Выберите спектр из частотной или уровневой серии SMU")
            return
        if kind == "matrix":
            groups = {}
            for spectrum in self.spectra:
                point = spectrum.get("context", {})
                if point.get("series_id") == series_id and point["rf_hz"] == context["rf_hz"]:
                    result = mixer_result(spectrum)
                    if result["status"] == "preliminary":
                        lo = point["lo_actual_dbm"] - point["lo_loss_db"]
                        groups.setdefault(lo, []).append((point["pin_dbm"], result["value_db"]))
            if not groups:
                self.error("Нет расчётных точек комплексной проверки для выбранной RF")
                return
            self.showing_series = True
            self.plot.clear()
            legend = self.plot.getPlotItem().legend or self.plot.addLegend()
            legend.clear()
            legend.show()
            self.label_axis("bottom", "Pin RF", "dBm", False)
            self.label_axis("left", "Потери преобразования", "dB", False)
            self.plot.setTitle(f"Комплексная · RF {context['rf_hz'] / 1e6:.6f} МГц · предварительно")
            for index, (lo, points) in enumerate(sorted(groups.items())):
                points.sort()
                self.plot.plot([item[0] for item in points], [item[1] for item in points],
                               pen=pg.intColor(index, hues=len(groups)), symbol="o", name=f"LO {lo:g} dBm")
            self.plot.enableAutoRange()
            self.marker_text.setText(f"{len(groups)} уровней LO. Чтобы сменить RF, выберите запись другой частоты в архиве.")
            return
        points = []
        for spectrum in self.spectra:
            point = spectrum.get("context", {})
            if point.get("series_id") == series_id:
                result = mixer_result(spectrum)
                if result["status"] == "preliminary":
                    x = (point["rf_hz"] if kind == "rf_frequency" else point["pin_dbm"] if kind == "rf_power"
                         else point["lo_actual_dbm"] - point["lo_loss_db"])
                    points.append((point["series_index"], x, result["value_db"]))
        if not points:
            self.error("В серии нет расчётных точек: проверьте флажки калибровки и условия измерений")
            return
        points.sort()
        self.showing_series = True
        self.plot.clear()
        self.label_axis("bottom", "RF" if kind == "rf_frequency" else "Pin RF" if kind == "rf_power" else "Уровень LO",
                        "Hz" if kind == "rf_frequency" else "dBm", kind == "rf_frequency")
        self.label_axis("left", "Потери / развязка", "dB", False)
        self.plot.setTitle("Кривая серии · предварительно")
        self.plot.plot([point[1] for point in points], [point[2] for point in points], pen="#45d7d0", symbol="o")
        self.plot.enableAutoRange()
        self.marker_text.setText(f"Расчётных точек: {len(points)}. Для возврата к спектру выберите запись архива.")

    def move_marker(self, position):
        if self.current is None or self.showing_series or not self.plot.sceneBoundingRect().contains(position):
            return
        mapped = self.plot.getViewBox().mapSceneToView(position)
        hz, power = self.frequency_axis, self.current["power_dbm"]
        index = min(len(hz) - 1, max(0, round((mapped.x() - hz[0]) / (hz[-1] - hz[0]) * (len(hz) - 1))))
        self.cross_x.setPos(hz[index])
        self.cross_y.setPos(power[index])
        self.cross_x.show()
        self.cross_y.show()
        self.marker_text.setText(f"Маркер: {hz[index] / 1e6:.6f} МГц · {power[index]:.3f} dBm · "
                                 f"от пика {power[index] - self.peak_power:.3f} дБ")

    def reload(self):
        try:
            self.spectra = self.store().load()
            blocker = QtCore.QSignalBlocker(self.table)
            self.table.setRowCount(len(self.spectra))
            method_labels = {key: label for label, key in METHOD_CHOICES}
            method_labels.update(two_tone_pa="PA · два тона", two_tone_mixer="Смеситель · два тона", harmonic_pa="PA · гармоническая полоса")
            for row, spectrum in enumerate(self.spectra):
                context = spectrum.get("context", {})
                result = mixer_result(spectrum)
                method = context.get("method", "spectrum")
                values = [context.get("sample", ""), context.get("batch", ""), context.get("model", ""),
                          spectrum["measured_at"][:19], method_labels.get(method, method),
                          f"{context.get('rf_hz', 0) / 1e6:.6f}", f"{context.get('lo_hz', 0) / 1e6:.6f}", f"{context.get('if_hz', 0) / 1e6:.6f}",
                          f"{context['rf_actual_dbm']:.3f}" if "rf_actual_dbm" in context else "",
                          f"{context['lo_actual_dbm']:.3f}" if "lo_actual_dbm" in context else "",
                          f"{result['value_db']:.3f}" if result.get("value_db") is not None else "—",
                          {"preliminary": "Предварительно", "not_calculated": "Нет расчёта", "spectrum_only": "Спектр"}[result["status"]],
                          context.get("notes", "")]
                for column, value in enumerate(values):
                    self.table.setItem(row, column, QtWidgets.QTableWidgetItem(value))
            self.table.clearSelection()
            self.filter_archive()
            del blocker
        except Exception as error:
            self.spectra = []
            self.table.setRowCount(0)
            self.error(str(error))

    def filter_archive(self):
        search = self.search.text().strip().casefold()
        visible = 0
        blocker = QtCore.QSignalBlocker(self.table)
        for row in range(self.table.rowCount()):
            values = [self.table.item(row, column).text() for column in range(self.table.columnCount())]
            matches = (not search or any(search in value.casefold() for value in values)) and all(
                values[column] in selected for column, selected in self.column_filters.items())
            self.table.setRowHidden(row, not matches)
            visible += int(matches)
            if not matches:
                self.table.selectionModel().select(self.table.model().index(row, 0),
                    QtCore.QItemSelectionModel.SelectionFlag.Deselect | QtCore.QItemSelectionModel.SelectionFlag.Rows)
        for column, label in enumerate(self.archive_headers):
            self.table.horizontalHeaderItem(column).setText(label + (" ● ▼" if column in self.column_filters else " ▼"))
            self.table.horizontalHeaderItem(column).setToolTip("Выбрать значения · " + label)
        self.filter_count.setText(f"Показано: {visible} / {self.table.rowCount()}")
        del blocker
        self.charts.invalidate()
        self.settings_changed.emit(self.settings())

    def show_archive_filter(self, column):
        from .ui import ColumnFilterDialog

        values = sorted({self.table.item(row, column).text() for row in range(self.table.rowCount())}, key=str.casefold)
        dialog = ColumnFilterDialog(self.archive_headers[column], values, self.column_filters.get(column), self)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            selected = dialog.selected_values()
            if selected == set(values):
                self.column_filters.pop(column, None)
            else:
                self.column_filters[column] = selected
            self.filter_archive()

    def clear_archive_filters(self):
        self.column_filters.clear()
        self.search.clear()
        self.filter_archive()

    def chart_spectra(self, selected=False):
        indices = {item.row() for item in self.table.selectionModel().selectedRows()} if selected else range(self.table.rowCount())
        return [self.spectra[index] for index in sorted(indices) if not self.table.isRowHidden(index)]

    def show_charts(self):
        self.views.setCurrentWidget(self.charts)

    def identify_akip(self):
        self.views.setCurrentWidget(self.two_tone)
        self.two_tone.launch("identify")

    def two_tone_busy(self, busy):
        self.form_panel.setEnabled(not busy)
        for button in self.operation_buttons:
            button.setEnabled(not busy)
        self.stop_button.setEnabled(busy or self.generator_active)

    def show_selected(self):
        self.charts.invalidate()
        rows = self.table.selectionModel().selectedRows()
        if rows and not self.table.isRowHidden(rows[0].row()):
            self.display(self.spectra[rows[0].row()])

    def choose_database(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "База смесителей", self.database.text(), "SQLite (*.db *.sqlite *.sqlite3)")
        if path:
            self.database.setText(path)

    def create_database(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Новая база смесителей", self.database.text(), "SQLite (*.db)")
        if path:
            try:
                SpectrumStore(path, False).initialize()
                self.database.setText(path)
                self.status.setText("База смесителей создана: " + path)
            except Exception as error:
                self.error(str(error))

    def export_csv(self):
        if not self.current:
            self.error("Сначала считайте или выберите спектр")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "CSV текущего спектра", "spectrum.csv", "CSV (*.csv)")
        if path:
            try:
                with open(path, "w", encoding="utf-8-sig", newline="") as stream:
                    writer = csv.writer(stream, delimiter=";")
                    context = self.current.get("context", {})
                    result = mixer_result(self.current)
                    writer.writerow(["spectrum_id", "mixer", "method", "RF_Hz", "LO_Hz", "IF_Hz",
                                     "frequency_Hz", "power_dBm", "result_status", "result_dB"])
                    writer.writerows((self.current["id"], context.get("sample", ""), context.get("method", "spectrum"),
                                      context.get("rf_hz", ""), context.get("lo_hz", ""), context.get("if_hz", ""),
                                      hz, power, result["status"], result.get("value_db", "")) for hz, power in
                                    zip(frequencies(self.current), self.current["power_dbm"]))
            except Exception as error:
                self.error(str(error))

    def export_series_csv(self):
        series_id = self.current.get("context", {}).get("series_id") if self.current else None
        if not series_id:
            self.error("Выберите спектр серии")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Сводка серии", "mixer-series.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            summary = self.summary_for(self.current)
            records = sorted((item for item in self.spectra if item.get("context", {}).get("series_id") == series_id),
                             key=lambda item: item["context"]["series_index"])
            with open(path, "w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.writer(stream, delimiter=";")
                writer.writerow(["series_id", "spectrum_id", "mixer", "index", "RF_Hz", "LO_Hz", "IF_Hz",
                    "RF_set_dBm", "LO_set_dBm", "Pin_dBm", "Pout_RSA_dBm", "Pout_DUT_dBm", "result_dB", "status",
                    "P1dB_in_dBm", "P1dB_out_dBm", "P1dB_status"])
                for item in records:
                    point, result = item["context"], mixer_result(item)
                    estimate = summary
                    if summary and summary["kind"] == "matrix_p1db":
                        estimate = next((value for value in summary["slices"] if value["rf_hz"] == point["rf_hz"] and
                                         value["lo_dbm"] == point["lo_actual_dbm"] - point["lo_loss_db"]), None)
                    p1db = (estimate.get("result") or {}) if estimate else {}
                    measured = result.get("marker", {}).get("power_dbm")
                    writer.writerow([series_id, item["id"], point["sample"], point["series_index"], point["rf_hz"],
                        point["lo_hz"], point["if_hz"], point.get("rf_set_dbm", ""), point.get("lo_set_dbm", ""),
                        point.get("pin_dbm", ""), measured if measured is not None else "",
                        measured + point["output_loss_db"] if measured is not None else "", result.get("value_db", ""), result["status"],
                        p1db.get("input_p1db_dbm", ""), p1db.get("output_p1db_dbm", ""), estimate["status"] if estimate else ""])
        except Exception as error:
            self.error(str(error))

    def export_json(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Архив спектров", "mixers.json", "JSON (*.json)")
        if path:
            try:
                self.store().export_json(path)
            except Exception as error:
                self.error(str(error))

    def import_json(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Архив спектров", "", "JSON (*.json)")
        if path:
            try:
                count = self.store().import_json(path)
                self.reload()
                self.status.setText(f"Добавлено спектров: {count}")
            except Exception as error:
                self.error(str(error))

    def recover(self):
        try:
            store = self.store()
            count = 0
            for path in (self.root / "data" / "rf-pending").glob("*.json"):
                pending = json.loads(path.read_text(encoding="utf-8"))
                if Path(pending["database"]).resolve() == store.path:
                    count += store.save_many([pending["spectrum"]])
                    path.unlink()
            self.reload()
            self.status.setText(f"Восстановлено спектров: {count}")
        except Exception as error:
            self.error(str(error))

    def stop(self):
        if self.two_tone.worker or self.two_tone.unsafe_outputs:
            self.two_tone.stop()
            return
        if self.worker:
            self.stop_pending = True
            self.worker.stop.set()
            self.status.setText("Останавливаю операцию; управляемые выходы SMU будут отключены с подтверждением")
        elif self.generator_active:
            self.launch("smu_off")

    def worker_finished(self):
        operation = self.worker.operation
        self.worker.deleteLater()
        self.worker = None
        self.form_panel.setEnabled(True)
        for button in self.operation_buttons:
            button.setEnabled(True)
        self.stop_button.setEnabled(self.generator_active)
        self.update_source_controls()
        if operation == "smu_off" and self.cleanup_error:
            if not self.last_error:
                self.last_error = self.cleanup_error
            self.cleanup_error = None
        if self.last_error:
            self.status.setText("ОШИБКА: " + self.last_error)
        elif self.saved_count:
            self.status.setText(f"Сохранено спектров: {self.saved_count} · {self.active_store.path}")
        if self.stop_pending and self.generator_active and operation != "smu_off":
            self.stop_pending = False
            original_error = self.last_error
            self.launch("smu_off")
            self.cleanup_error = original_error
        else:
            self.stop_pending = False

    def error(self, message):
        self.last_error = message
        if self.worker is not None and self.generator_active:
            self.stop_pending = True
        self.status.setText("ОШИБКА: " + message)
        self.log.appendPlainText("ОШИБКА: " + message)

    def closeEvent(self, event):
        if self.worker or self.generator_active or self.two_tone.worker or self.two_tone.unsafe_outputs:
            self.stop()
            event.ignore()
        else:
            self.settings_changed.emit(self.settings())
            event.accept()
