"""Two-tone mixer/PA acquisition with SMU200A, AKIP-3208 and RSA6265."""

import csv
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading
import uuid

from PySide6 import QtCore, QtWidgets
import pyqtgraph as pg
import pyqtgraph.exporters

from .rf_storage import SpectrumStore
from .uncertainty import result_uncertainty
from .rsa import AcquisitionCancelled, Rsa6265, average_spectra, demo_spectrum, frequencies
from .smu import Smu200a, SourcePlan, verified_off
from .ssg import Ssg3000x, verified_ssg_off
from .smr import lo_source, verified_lo_off
from .two_tone import TwoTonePlan, analyze_two_tone, harmonic_band, parse_frequency_ranges, parse_level_ranges
from .source_calibration import PowerCalibrationWorker, calibrated_source_plan
from .source_calibration_ui import PowerCalibrationPanel
from .form_layout import compact_inputs
from .validation import FieldError, FieldFeedback, StatusLabel
from .rf_labels import INPUT_ATTENUATION_A, INPUT_ATTENUATION_B, INPUT_ATTENUATION_HINT, RSA_ATTENUATION, SMU_LEVEL
from .two_tone_results import TwoToneResults, columns, row_values


class TwoToneWorker(QtCore.QThread):
    log = QtCore.Signal(str)
    status = QtCore.Signal(str)
    point_ready = QtCore.Signal(object)
    failed = QtCore.Signal(str)
    outputs = QtCore.Signal(object)

    def __init__(self, owner, settings, plans, operation="measure"):
        super().__init__(owner)
        self.settings, self.plans, self.operation = settings, plans, operation
        self.stop = threading.Event()

    def run(self):
        smu, akip, rsa = None, None, None
        owns = self.operation in ("measure", "off")
        needs_lo = self.operation == "identify" or self.settings.get("lo_required",
            self.operation == "off" or not self.plans or any(plan.mode == "mixer" for plan in self.plans))
        enable_attempted = False
        series = str(uuid.uuid4())
        try:
            if not self.settings["demo"]:
                if needs_lo:
                    akip = (lo_source(self.settings, self.log.emit) if self.settings.get("lo_source") == "smr" else
                        Ssg3000x(self.settings["akip_host"], self.settings["akip_port"], self.log.emit))
                if self.operation == "identify":
                    state = akip.state()
                    self.status.emit("Источник LO: " + json.dumps(state, ensure_ascii=False))
                    self.outputs.emit(dict(known=True, active=bool(state["output"])))
                    return
                smu = Smu200a(self.settings["smu_host"], self.settings["smu_port"], self.log.emit)
                profile = self.settings.get("power_calibration")
                if self.operation == "measure" and profile and smu.identity != profile["generator_identity"]:
                    raise ValueError("Профиль калибровки снят другим SMU; паспорт прибора не совпадает")
                smu.all_off()
                if akip:
                    akip.off()
            if self.operation in ("off", "identify"):
                self.status.emit("ДЕМО источников" if self.settings["demo"] else "Источники отключены")
                return
            for plan in self.plans:
                plan.validate()
            store = SpectrumStore(self.settings["database"], self.settings["demo"])
            store.initialize()
            if not self.settings["demo"]:
                rsa = Rsa6265(self.settings["rsa_host"], self.log.emit)
                first = self.plans[0]
                source_plan = self.source_plan(first)
                smu.prepare(source_plan)
                if first.mode == "mixer":
                    akip.prepare(first.lo_hz, first.lo_dbm)
                # Validate every level while both generators are still OFF.
                for plan in self.plans:
                    smu.validate_plan(self.source_plan(plan))
                    if plan.mode == "mixer":
                        akip.setpoint(plan.lo_hz, plan.lo_dbm)
            active = False
            previous = None
            for index, plan in enumerate(self.plans):
                if self.stop.is_set():
                    raise AcquisitionCancelled()
                if previous and plan.mode == "pa" and plan.f1_hz == self.plans[index - 1].f1_hz:
                    predicted = previous["result"]["output_average_w"] * 10 ** ((plan.level1_dbm - self.plans[index - 1].level1_dbm) / 10)
                    if predicted > plan.max_output_w:
                        self.status.emit("Следующий шаг превысит заданный предел мощности; серия завершена")
                        break
                generator, generator2 = {}, {}
                if smu:
                    generator = smu.setpoint(self.source_plan(plan))
                    generator2 = akip.setpoint(plan.lo_hz, plan.lo_dbm) if plan.mode == "mixer" else dict(output=0, identity="LO not used")
                    if not active:
                        enable_attempted = True
                        if plan.mode == "mixer":
                            generator2 = akip.enable(self.stop)
                        generator = smu.enable((1, 2), self.stop)
                        active = True
                    expected = {1, 2}
                    if ({int(channel) for channel, state in generator["channels"].items() if state["output"]} != expected
                            or bool(generator2["output"]) != (plan.mode == "mixer")):
                        raise ValueError("Состояние выходов не соответствует выбранной двухтоновой схеме")
                if self.stop.wait(self.settings["settling_s"]):
                    raise AcquisitionCancelled()
                spectrum = self.capture(rsa, plan, 1)
                if smu and (smu.state() != generator or (akip is not None and akip.state() != generator2)):
                    raise ValueError("Источники изменились во время развёртки")
                context = dict(sample=self.settings["sample"], batch=self.settings["batch"], model=self.settings["model"],
                    notes=self.settings["notes"], method="two_tone_" + plan.mode, rf_hz=plan.f1_hz,
                    lo_hz=plan.lo_hz if plan.mode == "mixer" else 0,
                    if_hz=abs(plan.lo_hz - plan.f1_hz) if plan.mode == "mixer" else plan.f1_hz,
                    rf_actual_dbm=plan.level1_dbm,
                    pin_dbm=plan.level1_dbm - plan.loss1_db, output_loss_db=plan.output_loss_db,
                    marker_window_hz=min(plan.rbw_hz * 3, plan.spacing_hz * 0.2),
                    two_tone_plan=asdict(plan), series_id=series, series_index=index,
                    sources="SMU200A A+B; " + (("SMR20" if self.settings.get("lo_source") == "smr" else "AKIP3208") + " LO" if plan.mode == "mixer" else "LO not used"),
                    source_layout="smu_ab_akip_lo", tone1_channel=1, tone2_channel=2,
                    rf_channel=1, lo_source=("SMR20" if self.settings.get("lo_source") == "smr" else "AKIP3208") if plan.mode == "mixer" else None,
                    lo_endpoint={key: self.settings[key] for key in ("lo_source", "smr_port", "smr_baud", "akip_host", "akip_port") if key in self.settings},
                    sweep_kind="two_tone_power")
                if self.settings.get("power_calibration"):
                    profile = self.settings["power_calibration"]
                    context["source_calibration"] = dict(id=profile["id"], method=profile["plan"]["method"],
                        f1_hz=profile["plan"]["f1_hz"], f2_hz=profile["plan"]["f2_hz"],
                        path=profile["path"], created_at=profile["created_at"], meter_identity=profile["meter_identity"],
                        commands=asdict(self.source_plan(plan)), tolerance_db=profile["plan"]["tolerance_db"],
                        points=[point for point in profile["points"] if any(math.isclose(point["target_dbm"], target,
                            abs_tol=1e-6, rel_tol=0) for target in (plan.level1_dbm - plan.loss1_db, plan.level2_dbm - plan.loss2_db))])
                    context["source_calibration"]["frequency_profiles"] = [dict(id=item["id"],
                        f1_hz=item["plan"]["f1_hz"], f2_hz=item["plan"]["f2_hz"], meter_identity=item["meter_identity"],
                        points=[point for point in item["points"] if any(math.isclose(point["target_dbm"], target,
                            abs_tol=1e-6, rel_tol=0) for target in (plan.level1_dbm - plan.loss1_db, plan.level2_dbm - plan.loss2_db))])
                        for item in self.settings.get("power_calibration_profiles", [])
                        if any(math.isclose(item["plan"]["f1_hz"], hz, abs_tol=0.05, rel_tol=0)
                            for hz in (plan.f1_hz, plan.f2_hz))]
                if plan.mode == "mixer":
                    context["lo_actual_dbm"] = plan.lo_dbm
                spectrum["context"], spectrum["generator"], spectrum["generator2"] = context, generator, generator2
                result = analyze_two_tone(spectrum)
                spectrum["result"] = result
                self.save(store, spectrum)
                result = spectrum["result"]
                if result["status"] != "preliminary":
                    raise ValueError(result["reason"])
                point = dict(spectrum_id=spectrum["id"], plan=asdict(plan), result=result, harmonics=[], demo=self.settings["demo"])
                if plan.mode == "pa" and result["output_average_w"] >= plan.max_output_w:
                    self.point_ready.emit(point)
                    self.status.emit("Измеренный предел мощности достигнут; серия остановлена")
                    break
                if plan.mode == "pa":
                    for order in range(2, plan.harmonics + 1):
                        if self.stop.is_set():
                            raise AcquisitionCancelled()
                        harmonic = self.capture(rsa, plan, order)
                        if smu and (smu.state() != generator or (akip is not None and akip.state() != generator2)):
                            raise ValueError("Источники изменились во время измерения гармоник")
                        band = harmonic_band(harmonic, plan, order)
                        fundamental_name = "fund_low" if result["fundamental_low_dbm"] >= result["fundamental_high_dbm"] else "fund_high"
                        fundamental = result["fundamental_low_dbm"] if fundamental_name == "fund_low" else result["fundamental_high_dbm"]
                        band["fundamental_dbm"] = fundamental
                        band["fundamental_frequency_hz"] = result["markers"][fundamental_name]["frequency_hz"]
                        band["dbc"] = band["output_dbm"] - fundamental if band["resolved"] else None
                        band["noise_limit_dbc"] = band["noise_dbm"] + 10 + plan.output_loss_db - fundamental
                        point["harmonics"].append(band)
                        harmonic["context"] = dict(context, method="harmonic_pa", harmonic_order=order,
                            harmonic_result=band, fundamental_total_w=result["output_average_w"], parent_spectrum_id=spectrum["id"])
                        harmonic["generator"], harmonic["generator2"] = generator, generator2
                        harmonic["result"] = dict(kind="harmonic", status="preliminary", value_db=-band["dbc"] if band["dbc"] is not None else None, marker=dict(
                            frequency_hz=band["frequency_hz"], power_dbm=band["rsa_dbm"]))
                        self.save(store, harmonic)
                self.point_ready.emit(point)
                previous = point
                self.status.emit(f"Двухтоновая точка {index + 1}/{len(self.plans)} сохранена")
        except (AcquisitionCancelled, InterruptedError):
            self.status.emit("Остановлено; отключаю оба генератора")
        except Exception as error:
            self.failed.emit(str(error))
        finally:
            if owns:
                errors = []
                # Always attempt both instruments even if one fails.
                for driver, off, label in ((akip, verified_lo_off, "Источник LO"), (smu, verified_off, "SMU")):
                    if label == "Источник LO" and not needs_lo:
                        continue
                    if driver:
                        try:
                            off(driver)
                        except Exception as error:
                            errors.append(label + ": " + str(error))
                    elif not self.settings["demo"]:
                        errors.append(label + ": связь не установлена, OFF не подтверждён")
                must_confirm_off = enable_attempted or self.operation == "off"
                if errors:
                    self.outputs.emit(dict(known=False, active=must_confirm_off, requires_off=must_confirm_off))
                else:
                    self.outputs.emit(dict(known=True, active=False))
                if errors and must_confirm_off:
                    self.failed.emit("Отключение не подтверждено: " + "; ".join(errors))
                elif errors:
                    self.log.emit("RF не включался; ошибка связи не требует блокировки настроек.")
            for driver in (rsa, akip, smu):
                if driver:
                    try:
                        driver.close()
                    except Exception as error:
                        self.failed.emit("Закрытие связи: " + str(error))

    def source_plan(self, plan):
        if self.settings.get("power_calibration"):
            return calibrated_source_plan(self.settings["power_calibration"], plan, self.settings)
        return SourcePlan(plan.f1_hz, plan.f2_hz, plan.level1_dbm, plan.level2_dbm, 1, 2)

    def capture(self, rsa, plan, order):
        if plan.sweep_averages > 1:
            single = replace(plan, sweep_averages=1)
            sweeps = []
            for index in range(plan.sweep_averages):
                if self.stop.is_set():
                    raise AcquisitionCancelled()
                self.status.emit(f"H{order}: развёртка {index + 1}/{plan.sweep_averages}")
                sweeps.append(self.capture(rsa, single, order))
            return average_spectra(sweeps, dict(method="spectrum"))
        config = plan.analyzer_config(order)
        actual = rsa.configure(config) if rsa else None
        spectrum = rsa.acquire(self.stop) if rsa else demo_spectrum(config)
        if actual and spectrum["settings"] != actual:
            raise ValueError("Настройки RSA изменились во время измерения")
        if spectrum.get("if_overload"):
            raise ValueError("RSA сообщил о перегрузке ПЧ")
        if rsa and max(spectrum["power_dbm"]) >= spectrum["settings"]["reference_dbm"] - 0.5:
            raise ValueError("Пик достигает опорного уровня RSA")
        if not rsa:
            low, high = sorted(plan.output_tones())
            pin = plan.level1_dbm - plan.loss1_db
            gain = 25 if plan.mode == "pa" else -6
            compression = max(0, pin + 10) * 0.2
            fundamental = pin + gain - compression - plan.output_loss_db
            targets = ((low, fundamental), (high, fundamental),
                (2 * low - high, fundamental - max(5, 45 - 2 * (pin + 26))),
                (2 * high - low, fundamental - max(5, 46 - 2 * (pin + 26)))) if order == 1 else (
                (order * low, fundamental - 35 - order * 2), (order * high, fundamental - 37 - order * 2))
            spectrum["power_dbm"] = [max([-110] + [level - min(100, ((hz - frequency) / (plan.rbw_hz / 2)) ** 2 * 3)
                for frequency, level in targets]) for hz in frequencies(spectrum)]
        return spectrum

    def save(self, store, spectrum):
        pending = Path(self.settings["root"]) / "data" / "rf-pending" / (spectrum["id"] + ".json")
        pending.parent.mkdir(parents=True, exist_ok=True)
        pending.write_text(json.dumps(dict(database=str(store.path), spectrum=spectrum), ensure_ascii=False, indent=2), encoding="utf-8")
        store.save_many([spectrum])
        pending.unlink()


class TwoTonePanel(QtWidgets.QWidget):
    settings_changed = QtCore.Signal()
    busy_changed = QtCore.Signal(bool)

    def __init__(self, bench):
        super().__init__(bench)
        self.bench = bench
        self.worker = None
        self.unsafe_outputs = False
        self.last_settings = None
        self.points = []
        self.fields = {}
        layout = QtWidgets.QHBoxLayout(self)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(300)
        scroll.setMaximumWidth(360)
        layout.addWidget(scroll)
        self.pages = QtWidgets.QTabWidget()
        layout.addWidget(self.pages, 1)
        measurement_page = QtWidgets.QWidget()
        right = QtWidgets.QVBoxLayout(measurement_page)
        self.pages.addTab(measurement_page, "Двухтоновые измерения")
        self.form_widget = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(self.form_widget)
        self.form = form
        form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        scroll.setWidget(self.form_widget)

        def add(key, label, value, minimum=None, maximum=None, decimals=2, place=True):
            if minimum is not None:
                widget = QtWidgets.QDoubleSpinBox()
                widget.setRange(minimum, maximum)
                widget.setDecimals(decimals)
                widget.setValue(value)
            elif isinstance(value, bool):
                widget = QtWidgets.QCheckBox(label)
                widget.setChecked(value)
            else:
                widget = value
            self.fields[key] = widget
            if not place:
                return widget
            if isinstance(widget, QtWidgets.QCheckBox):
                widget.setAccessibleName(label)
                widget.setText("")
                row = QtWidgets.QWidget()
                row_layout = QtWidgets.QHBoxLayout(row)
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.addWidget(widget)
                caption = QtWidgets.QLabel(label)
                caption.setWordWrap(True)
                row_layout.addWidget(caption, 1)
                form.addRow(row)
            else:
                form.addRow(label, widget)
            return widget

        def paired_row(title, frequency_label, frequency, level_label, level):
            group = QtWidgets.QGroupBox(title)
            grid = QtWidgets.QGridLayout(group)
            for column, (label, widget) in enumerate(((frequency_label, frequency), (level_label, level))):
                caption = QtWidgets.QLabel(label)
                caption.setWordWrap(True)
                grid.addWidget(caption, 0, column)
                grid.addWidget(widget, 1, column)
                grid.setColumnStretch(column, 1)
            form.addRow(group)
            return group

        self.mode = QtWidgets.QComboBox()
        self.mode.addItem("PA · два тона и гармонические полосы", "pa")
        self.mode.addItem("Смеситель · два RF-тона и LO", "mixer")
        add("mode", "Объект", self.mode)
        first = add("f1_mhz", "Первый тон, МГц", 14, 0.1, 200, 6, place=False)
        level1 = add("level1", "Уровень первого тона, dBm", -20, -110, 20, place=False)
        paired_row("Первый тон · SMU A/1", "Частота, МГц", first, SMU_LEVEL, level1)
        self.f2_mhz = QtWidgets.QLineEdit()
        self.f2_mhz.setReadOnly(True)
        self.f2_mhz.setToolTip("Частота второго тона = первый тон + интервал. Измените первый тон или интервал ниже.")
        level2 = add("level2", "Уровень второго тона, dBm", -20, -110, 20, place=False)
        paired_row("Второй тон · SMU B/2", "Частота, МГц", self.f2_mhz, SMU_LEVEL, level2)
        add("spacing_hz", "Интервал тонов, Гц", 2000, 100, 100000, 0)
        lo = add("lo_mhz", "LO для смесителя, МГц", 25.0592, 0.1, 3070, 6, place=False)
        lo_level = add("lo_dbm", "Уровень источника LO, dBm", 13, -110, 20, place=False)
        self.lo_group = paired_row("LO смесителя · отдельный генератор", "Частота, МГц", lo, "Уровень, dBm", lo_level)
        frequency_ranges = QtWidgets.QPlainTextEdit()
        frequency_ranges.setFixedHeight(100)
        frequency_ranges.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        frequency_ranges.setPlaceholderText("14; 14.175; 14.35\n50..52/1")
        frequency_ranges.setToolTip(
            "ЧАСТОТЫ ПЕРВОГО ТОНА, МГц\n\n"
            "Отдельные частоты:\n14; 14.175; 14.35\n"
            "Это три измерения: 14,000 / 14,175 / 14,350 МГц.\n\n"
            "Диапазон с шагом:\n50..52/1\n"
            "Это 50 / 51 / 52 МГц: от 50 до 52 с шагом 1 МГц.\n\n"
            "Можно сочетать строки:\n1.810; 1.905; 2\n50..52/1\n\n"
            "Когда поле заполнено, оно задаёт частоты первого тона.\n"
            "Верхние поля частот показывают первую пару серии.\n"
            "Второй тон всегда выше первого на заданный интервал.\n"
            "Например, при интервале 2000 Гц: 14,000 и 14,002 МГц.\n"
            "Пустое поле: одна пара с частотой из верхнего поля.\n"
            "Уровни обоих тонов сверху действуют для всей серии.\n"
            "Для смесителя в частотной серии LO = первый тон + ПЧ.")
        self.fields["frequency_ranges"] = frequency_ranges
        form.addRow(QtWidgets.QLabel("Частоты первого тона / диапазоны и шаг, МГц"))
        form.addRow(frequency_ranges)
        self.frequency_summary = QtWidgets.QLabel()
        self.frequency_summary.setWordWrap(True)
        form.addRow(self.frequency_summary)
        self.single_frequency_mhz = first.value()
        frequency_ranges.textChanged.connect(self.update_frequency_mode)
        add("if_mhz", "ПЧ для частотной серии, МГц", 11.0592, 0.1, 3000, 6)
        self.fields["if_mhz"].setToolTip("Для каждой частоты серии задаётся LO = RF + ПЧ. Одиночное измерение использует частоту LO выше.")
        lo_levels = QtWidgets.QLineEdit()
        lo_levels.setPlaceholderText("например: 10..20/1")
        lo_levels.setToolTip("Необязательная серия уровня LO. Формат диапазон начало..конец/шаг или список через ';'. "
            "Число точек умножается на частоты и уровни RF.")
        add("lo_levels", "Уровни LO, dBm", lo_levels)
        first.valueChanged.connect(self.update_second_tone)
        self.fields["spacing_hz"].valueChanged.connect(self.update_second_tone)
        self.update_second_tone()
        add("loss1", INPUT_ATTENUATION_A, 6, 0, 100)
        add("loss2", INPUT_ATTENUATION_B, 6, 0, 100)
        for key, channel in (("loss1", "A/1"), ("loss2", "B/2")):
            self.fields[key].setToolTip(INPUT_ATTENUATION_HINT.format(channel=channel))
        add("output_loss", RSA_ATTENUATION, 40, 0, 100)
        add("pad_rating", "Мощность первого аттенюатора, Вт", 10, 0, 100)
        add("max_w", "Предел средней мощности PA, Вт", 7, 0.001, 7, 3)
        add("harmonics", "Гармонические полосы до H", 5, 1, 7, 0)
        add("calibrated", "Поправки трактов проверены на частотах измерений", False)
        add("wiring", "Схема подключена: сумматор, нагрузки и аттенюаторы проверены", False)
        add("sweep", "Серия: повышать оба тона одинаковыми шагами", False)
        add("drive_stop", "Последний уровень тона 1, dBm", -10, -110, 20)
        add("drive_step", "Шаг уровней, дБ", 1, 0.1, 20)
        add("sweep_averages", "Независимых развёрток на полосу", 5, 1, 50, 0)
        for widget in self.fields.values():
            signal = (widget.currentIndexChanged if isinstance(widget, QtWidgets.QComboBox) else
                widget.toggled if isinstance(widget, QtWidgets.QCheckBox) else
                widget.textChanged if isinstance(widget, (QtWidgets.QLineEdit, QtWidgets.QPlainTextEdit)) else widget.valueChanged)
            signal.connect(lambda *args: self.settings_changed.emit())
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.wiring = QtWidgets.QLabel()
        self.wiring.setWordWrap(True)
        form.addRow(self.wiring)
        buttons = QtWidgets.QGridLayout()
        right.addLayout(buttons)
        self.preset = QtWidgets.QPushButton("Предустановка RSA для двух тонов")
        self.preset.clicked.connect(self.analyzer_preset)
        buttons.addWidget(self.preset, 0, 0)
        self.start_button = QtWidgets.QPushButton("Измерить два тона")
        self.start_button.clicked.connect(self.start)
        buttons.addWidget(self.start_button, 1, 0)
        self.driver_preset_button = QtWidgets.QPushButton("Шаблон BGA616 · 14 МГц")
        self.driver_preset_button.clicked.connect(self.driver_preset)
        buttons.addWidget(self.driver_preset_button, 0, 1)
        self.stop_button = QtWidgets.QPushButton("СТОП · оба генератора OFF")
        self.stop_button.clicked.connect(self.stop)
        buttons.addWidget(self.stop_button, 1, 1)
        self.results = TwoToneResults(self)
        self.plot = self.results.plot
        self.table = self.results.table
        right.addWidget(self.results, 1)
        self.results.metric_selector.currentIndexChanged.connect(lambda: self.settings_changed.emit())
        self.results.x_axis.currentIndexChanged.connect(lambda: self.settings_changed.emit())
        self.results.splitter.splitterMoved.connect(lambda: self.settings_changed.emit())
        tools = QtWidgets.QHBoxLayout()
        right.addLayout(tools)
        for title, callback in (("Мощность и PEP", self.plot_power), ("IM3 и гармоники", self.plot_distortion),
                                ("Сбросить масштаб", lambda: self.plot.enableAutoRange()),
                                ("Из архива", self.load_archive), ("PNG…", self.export_png), ("CSV точек…", self.export_csv)):
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(callback)
            tools.addWidget(button)
        self.status = StatusLabel("Данные пока не измерены. Пороговый вердикт не задаётся.")
        self.status.setWordWrap(True)
        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)
        right.addWidget(self.summary)
        right.addWidget(self.status)
        self.manual_off_button = QtWidgets.QPushButton("Подтвердить RF OFF обоих генераторов вручную")
        self.manual_off_button.setToolTip("Только после физической проверки: оба выхода SMU и RF-выход выбранного источника LO отключены.")
        self.manual_off_button.clicked.connect(self.confirm_manual_off)
        self.manual_off_button.hide()
        right.addWidget(self.manual_off_button)
        self.calibration = PowerCalibrationPanel(self)
        for key in ("loss1", "loss2"):
            self.fields[key].valueChanged.connect(self.calibration.fields[key].setValue)
            self.calibration.fields[key].valueChanged.connect(self.fields[key].setValue)
            self.fields[key].valueChanged.connect(lambda: self.fields["wiring"].setChecked(False))
        self.feedback = FieldFeedback(dict(self.bench.fields, **self.fields))
        self.pages.addTab(self.calibration, "Калибровка уровней SMU")
        self.calibration.changed.connect(lambda: self.settings_changed.emit())
        for key in ("f1_mhz", "spacing_hz", "loss1", "loss2", "frequency_ranges", "if_mhz"):
            widget = self.fields[key]
            signal = widget.textChanged if isinstance(widget, (QtWidgets.QLineEdit, QtWidgets.QPlainTextEdit)) else widget.valueChanged
            signal.connect(lambda: self.calibration.confirm.setChecked(False))
        self.bench.demo.toggled.connect(self.calibration.reload)
        self.calibration.reload()
        self.mode_changed()
        compact_inputs(self.form_widget)

    def settings(self):
        return dict(source_layout="smu_ab_akip_lo", power_calibration=self.calibration.settings(), results_view=self.results.settings(),
            single_frequency_mhz=self.single_frequency_mhz,
            **{key: widget.currentData() if isinstance(widget, QtWidgets.QComboBox) else
                widget.isChecked() if isinstance(widget, QtWidgets.QCheckBox) else
                widget.text().strip() if isinstance(widget, QtWidgets.QLineEdit) else
                widget.toPlainText().strip() if isinstance(widget, QtWidgets.QPlainTextEdit) else widget.value()
                for key, widget in self.fields.items() if key != "wiring"})

    def update_second_tone(self):
        if not self.fields["frequency_ranges"].toPlainText().strip():
            self.single_frequency_mhz = self.fields["f1_mhz"].value()
        frequency = self.fields["f1_mhz"].value() + self.fields["spacing_hz"].value() / 1e6
        self.f2_mhz.setText(f"{frequency:.6f}".replace(".", ","))
        self.update_frequency_mode()

    def update_frequency_mode(self):
        text = self.fields["frequency_ranges"].toPlainText().strip()
        first = self.fields["f1_mhz"]
        first.setEnabled(not bool(text))
        blocker = QtCore.QSignalBlocker(first)
        if not text:
            first.setValue(self.single_frequency_mhz)
            frequency = first.value() + self.fields["spacing_hz"].value() / 1e6
            self.f2_mhz.setText(f"{frequency:.6f}".replace(".", ","))
            self.frequency_summary.setText("Одна пара: частоту первого тона задаёт поле сверху. Второй = первый + интервал.")
        else:
            try:
                frequencies_hz = parse_frequency_ranges(text)
                if not frequencies_hz:
                    raise ValueError("Укажите частоты или очистите поле")
                first.setValue(frequencies_hz[0] / 1e6)
                second = frequencies_hz[0] / 1e6 + self.fields["spacing_hz"].value() / 1e6
                self.f2_mhz.setText(f"{second:.6f}".replace(".", ","))
                self.frequency_summary.setText(f"Серия: {len(frequencies_hz)} частот. Сверху показана первая пара: "
                    f"{first.value():g} / {second:g} МГц. Уровни сверху применяются ко всем парам.")
            except ValueError as error:
                self.f2_mhz.setText("—")
                self.frequency_summary.setText("Ошибка списка частот: " + str(error))
        del blocker

    def restore(self, settings):
        for key, value in settings.items():
            if key not in self.fields or key == "wiring":
                continue
            widget = self.fields[key]
            blocker = QtCore.QSignalBlocker(widget)
            if isinstance(widget, QtWidgets.QComboBox):
                widget.setCurrentIndex(max(0, widget.findData(value)))
            elif isinstance(widget, QtWidgets.QCheckBox):
                widget.setChecked(bool(value))
            elif isinstance(widget, QtWidgets.QPlainTextEdit):
                widget.setPlainText(str(value))
            elif isinstance(widget, QtWidgets.QLineEdit):
                widget.setText(str(value))
            else:
                widget.setValue(value)
            del blocker
        self.single_frequency_mhz = settings.get("single_frequency_mhz", settings.get("f1_mhz", 14))
        self.mode_changed(reset=False)
        self.update_second_tone()
        if settings.get("source_layout") != "smu_ab_akip_lo":
            self.fields["calibrated"].setChecked(False)
        self.calibration.restore(settings.get("power_calibration", {}))
        self.results.restore(settings.get("results_view", {}))

    def mode_changed(self, *args, reset=True):
        pa = self.mode.currentData() == "pa"
        self.fields["lo_mhz"].setEnabled(not pa)
        self.fields["lo_dbm"].setEnabled(not pa)
        self.form.setRowVisible(self.lo_group, not pa)
        for key in ("pad_rating", "max_w", "harmonics"):
            self.fields[key].setEnabled(pa)
            self.form.setRowVisible(self.fields[key], pa)
        self.fields["if_mhz"].setEnabled(not pa)
        self.form.setRowVisible(self.fields["if_mhz"], not pa)
        self.fields["lo_levels"].setEnabled(not pa)
        self.form.setRowVisible(self.fields["lo_levels"], not pa)
        if reset:
            self.fields["output_loss"].setValue(40 if pa else 0)
        self.fields["wiring"].setChecked(False)
        self.wiring.setText("PA: SMU A/1 и B/2 → резистивный сумматор → PA → 20 дБ / 10 Вт → ещё 20 дБ → RSA. "
            "Отдельный источник LO выключен. Ослабления трактов A/B включают кабели, аттенюаторы и комбинер. "
            "Гармоники означают максимум в полосе n·f1…n·f2, это не THD." if pa else
            "Смеситель: SMU A/1 и B/2 → сумматор → RF; выбранный генератор → LO; IF → RSA. "
            "Собственные IM3 источников и сумматора нужно измерить без DUT для проверки динамического диапазона стенда.")

    def analyzer_preset(self):
        for widget in (self.bench.rbw_hz, self.bench.vbw_hz):
            widget.setCurrentIndex(widget.findData(100))
        self.bench.attenuation_db.setValue(20)
        self.bench.reference_dbm.setValue(10)
        self.bench.points.setValue(3201)
        self.status.setText("Настройки RSA заполнены; приборы пока не менялись.")

    def driver_preset(self):
        self.mode.setCurrentIndex(self.mode.findData("pa"))
        self.calibration.use_profile.setChecked(False)
        self.fields["calibrated"].setChecked(False)
        self.fields["loss1"].setValue(16)
        self.fields["loss2"].setValue(16)
        self.fields["output_loss"].setValue(20)
        self.fields["max_w"].setValue(0.05)
        self.fields["level1"].setValue(6)
        self.fields["level2"].setValue(6)
        self.fields["drive_stop"].setValue(16)
        self.fields["drive_step"].setValue(1)
        self.fields["sweep"].setChecked(True)
        self.fields["frequency_ranges"].setPlainText("14; 14.175; 14.35")
        self.fields["spacing_hz"].setValue(2000)
        self.fields["harmonics"].setValue(7)
        self.fields["sweep_averages"].setValue(5)
        for widget in (self.bench.rbw_hz, self.bench.vbw_hz):
            widget.setCurrentIndex(widget.findData(100))
        self.bench.detector.setCurrentIndex(self.bench.detector.findData("RMS"))
        self.bench.attenuation_db.setValue(10)
        self.bench.reference_dbm.setValue(0)
        self.bench.points.setValue(3201)
        self.fields["wiring"].setChecked(False)
        self.status.setText("Шаблон BGA616: вход −10…0 dBm/тон, SMU +6…+16 dBm, 14 МГц, H7, ×5. Проверьте ослабления и схему.")

    def plans(self):
        data = self.settings()
        plan = TwoTonePlan(mode=data["mode"], f1_hz=data["f1_mhz"] * 1e6, spacing_hz=data["spacing_hz"],
            lo_hz=data["lo_mhz"] * 1e6, level1_dbm=data["level1"], level2_dbm=data["level2"],
            lo_dbm=data["lo_dbm"], loss1_db=data["loss1"], loss2_db=data["loss2"],
            output_loss_db=data["output_loss"], pad_rating_w=data["pad_rating"], max_output_w=data["max_w"],
            rbw_hz=self.bench.rbw_hz.currentData(), attenuation_db=self.bench.attenuation_db.value(),
            reference_dbm=self.bench.reference_dbm.value(), points=self.bench.points.value(),
            harmonics=int(data["harmonics"]) if data["mode"] == "pa" else 1, calibrated=data["calibrated"],
            sweep_averages=int(data.get("sweep_averages", 5)), detector=self.bench.detector.currentData())
        frequencies_hz = parse_frequency_ranges(data.get("frequency_ranges", "")) or [plan.f1_hz]
        lo_levels = ([data["lo_dbm"]] if data["mode"] == "pa" else
            parse_level_ranges(data.get("lo_levels", ""), data["lo_dbm"]))
        levels = [data["level1"]]
        if data["sweep"]:
            if data["drive_stop"] <= data["level1"]:
                raise ValueError("Последний уровень должен быть выше первого")
            level_count = math.ceil((data["drive_stop"] - data["level1"]) / data["drive_step"]) + 1
            levels = [min(data["drive_stop"], data["level1"] + index * data["drive_step"])
                for index in range(level_count)]
        if len(frequencies_hz) * len(levels) * len(lo_levels) > 201:
            raise ValueError("В серии допускается до 201 комбинации частоты, RF- и LO-уровней")
        frequency_series = bool(data.get("frequency_ranges", "").strip())
        result = []
        for frequency_hz in frequencies_hz:
            lo_hz = (frequency_hz + data["if_mhz"] * 1e6
                if frequency_series and data["mode"] == "mixer" else plan.lo_hz)
            for lo_dbm in lo_levels:
                for level1_dbm in levels:
                    level2_dbm = data["level2"] + level1_dbm - data["level1"] if data["sweep"] else data["level2"]
                    result.append(replace(plan, f1_hz=frequency_hz, lo_hz=lo_hz, lo_dbm=lo_dbm,
                        level1_dbm=level1_dbm, level2_dbm=level2_dbm))
        for item in result:
            item.validate()
        return result

    def start(self):
        self.launch("calibrate" if self.pages.currentIndex() == 1 else "measure")

    def launch(self, operation):
        if getattr(self.window(), "detector_busy", lambda: False)():
            self.status.setText("Сначала завершите калибровку детектора и подтвердите RF OFF")
            return
        if self.worker:
            return
        try:
            self.feedback.clear()
            self.calibration.feedback.clear()
            if self.bench.worker is not None or (operation in ("measure", "calibrate") and self.bench.generator_active):
                raise ValueError("Сначала остановите основное измерение и отключите управляемые выходы SMU")
            plans = self.plans() if operation == "measure" else []
            if operation == "measure" and not self.bench.demo.isChecked() and not self.fields["wiring"].isChecked():
                raise FieldError("Сначала подключите схему и подтвердите сумматор, нагрузку и выходные аттенюаторы", "wiring")
            data = self.bench.settings()
            if operation == "measure" and data.get("lo_source") == "smr" and any(
                    plan.mode == "mixer" and not 10e6 <= plan.lo_hz <= 20e9 for plan in plans):
                raise FieldError("SMR20: частота LO должна быть от 10 МГц до 20 ГГц", "lo_mhz", "if_mhz", "frequency_ranges")
            settings = dict(root=str(self.bench.root), database=str(self.bench.store().path), demo=data["demo"],
                lo_required=self.mode.currentData() == "mixer",
                lo_source=data.get("lo_source", "akip"), smr_port=data.get("smr_port", "COM3"), smr_baud=data.get("smr_baud", 19200),
                smu_host=data["smu_host"], smu_port=data["smu_port"], akip_host=data["akip_host"], akip_port=data["akip_port"],
                rsa_host=data["host"], rf_channel=data["rf_channel"], lo_channel=data["lo_channel"],
                sample=data["sample"], batch=data["batch"], model=data["model"], notes=data["notes"], settling_s=data["settling_s"])
            calibration = self.calibration.settings()
            settings.update(calibration_path=calibration["path"], nrp_resource=calibration["nrp_resource"],
                nrp_averages=int(calibration["nrp_averages"]),
                nrp_settling_s=calibration["nrp_settling_s"],
                rsa_attenuation=int(self.bench.attenuation_db.value()), rsa_reference=self.bench.reference_dbm.value())
            if operation == "measure" and calibration["use"]:
                settings["power_calibration"] = self.calibration.profile_for_plan(plans[0])
                settings["power_calibration_profiles"] = [profile for profile in self.calibration.available
                    if profile.get("calibration_batch_id", profile["id"]) == settings["power_calibration"].get(
                        "calibration_batch_id", settings["power_calibration"]["id"])]
                for plan in plans:
                    calibrated_source_plan(settings["power_calibration"], plan, settings)
            if operation == "calibrate":
                calibration_plan = self.calibration.plan()
                if not calibration["path"]:
                    raise FieldError("Укажите название тракта калибровки", "path")
                if not data["demo"] and not self.calibration.confirm.isChecked():
                    raise FieldError("Подтвердите в калибровке: DUT отключён, измеритель подключён вместо DUT, ослабления проверены", "confirm")
                if calibration_plan.method == "nrp" and not calibration["nrp_resource"]:
                    raise FieldError("Укажите VISA USB-ресурс NRP18T", "nrp_resource")
                self.calibration.prepare_run()
            if operation == "off" and self.last_settings:
                settings = self.last_settings
            if operation == "measure":
                self.points = []
                self.results.update_points()
            self.last_settings = settings
            self.form_widget.setEnabled(False)
            self.start_button.setEnabled(False)
            self.preset.setEnabled(False)
            self.driver_preset_button.setEnabled(False)
            self.calibration.set_busy(True)
            self.busy_changed.emit(True)
            self.worker = (PowerCalibrationWorker(self, settings, calibration_plan) if operation == "calibrate"
                else TwoToneWorker(self, settings, plans, operation))
            self.worker.log.connect(self.bench.log.appendPlainText)
            self.worker.status.connect(self.status.setText)
            if operation == "calibrate":
                self.worker.status.connect(self.calibration.status.setText)
                self.worker.failed.connect(self.calibration.error)
                self.worker.field_failed.connect(self.calibration.error)
            self.worker.failed.connect(self.error)
            self.worker.outputs.connect(self.outputs_updated)
            if operation == "calibrate":
                self.worker.profile_ready.connect(self.calibration.received)
            else:
                self.worker.point_ready.connect(self.received)
            self.worker.finished.connect(self.finished)
            if operation == 'identify':
                self.bench.lo_connection_button.watch(self.worker, settings['demo'])
            self.worker.start()
        except Exception as error:
            self.error(error)
            if operation == "calibrate" or getattr(error, "fields", ()) == ("profiles",):
                self.pages.setCurrentIndex(1)
                self.calibration.error(error)

    def error(self, error):
        self.status.setText("ОШИБКА: " + str(error))
        self.feedback.show(error)

    def outputs_updated(self, state):
        self.unsafe_outputs = state.get("requires_off", not state["known"] or state["active"])
        if self.worker and self.worker.operation in ("measure", "off", "calibrate") and not self.unsafe_outputs and state["known"]:
            self.bench.generator_active = False
            self.bench.smu_status.setText("SMU: RF OFF подтверждён; отдельный источник LO в калибровке не управлялся"
                if self.worker.operation == "calibrate" else "SMU: RF OFF подтверждён; LO не используется"
                if self.worker.plans and all(plan.mode == "pa" for plan in self.worker.plans)
                else "SMU и выбранный источник LO: RF OFF подтверждён")

    def received(self, point):
        self.points.append(point)
        result = point["result"]
        frequency_count = len({item["plan"]["f1_hz"] for item in self.points})
        lo_count = len({item["plan"]["lo_dbm"] for item in self.points})
        uncertainty = result.get("uncertainty", {})
        uncertainty_text = (f"U результата {uncertainty['expanded_uncertainty_k2_db']:.3f} дБ (k=2)."
            if uncertainty.get("expanded_uncertainty_k2_db") is not None else "U результата не рассчитана.")
        power_u = uncertainty.get("output_power") or {}
        power_text = (f" Pср U={power_u['average_power_expanded_db_k2']:.3f} дБ, "
            f"PEP U={power_u['pep_expanded_db_k2']:.3f} дБ (k=2)."
            if "average_power_expanded_db_k2" in power_u and "pep_expanded_db_k2" in power_u else "")
        self.summary.setText(f"Точек: {len(self.points)} · частот: {frequency_count} · "
            f"уровней LO: {lo_count} · "
            f"Pср по основным тонам {result['output_average_w']:.4g} Вт · "
            f"PEP по основным тонам {result['output_pep_w']:.4g} Вт. "
            + ("Поправки подтверждены; IP3 — экстраполяционная оценка. " if result["calibrated"] else "IP3 оценён по номинальным ослаблениям. ")
            + uncertainty_text + power_text)
        self.results.update_points()

    def stop(self):
        if self.worker:
            self.worker.stop.set()
        elif self.unsafe_outputs:
            self.launch("off")

    def finished(self):
        calibrated = self.worker.operation == "calibrate"
        calibration_summary = getattr(self.worker, "summary_text", None)
        self.worker.deleteLater()
        self.worker = None
        self.form_widget.setEnabled(not self.unsafe_outputs)
        self.start_button.setEnabled(not self.unsafe_outputs)
        self.preset.setEnabled(not self.unsafe_outputs)
        self.driver_preset_button.setEnabled(not self.unsafe_outputs)
        self.calibration.set_busy(self.unsafe_outputs)
        if calibrated:
            profile = getattr(self.calibration, "current_profile", None)
            self.calibration.reload()
            if profile:
                self.calibration.profiles.setCurrentIndex(self.calibration.profiles.findData(profile["id"]))
                self.calibration.received(profile)
            if calibration_summary:
                self.calibration.status.setText(calibration_summary)
        self.busy_changed.emit(self.unsafe_outputs)
        self.manual_off_button.setVisible(self.unsafe_outputs)
        self.bench.reload()
        if self.unsafe_outputs:
            self.status.setText("Отключение источников не подтверждено. Повторите СТОП / OFF и проверьте приборы.")

    def confirm_manual_off(self):
        if self.worker:
            return
        self.unsafe_outputs = False
        self.bench.generator_active = False
        self.form_widget.setEnabled(True)
        self.start_button.setEnabled(True)
        self.preset.setEnabled(True)
        self.driver_preset_button.setEnabled(True)
        self.calibration.set_busy(False)
        self.manual_off_button.hide()
        self.busy_changed.emit(False)
        self.status.setText("RF OFF обоих генераторов подтверждён пользователем; блокировка снята.")
        self.bench.log.appendPlainText(self.status.text())

    def plot_power(self):
        self.results.show_metric("power")

    def plot_distortion(self):
        self.results.show_metric("distortion")

    def load_archive(self):
        if self.worker:
            self.status.setText("Дождитесь завершения измерения перед загрузкой архивных точек.")
            return
        spectra = self.bench.chart_spectra()
        fundamentals = [item for item in spectra if item.get("context", {}).get("method") in ("two_tone_pa", "two_tone_mixer")]
        if not fundamentals:
            self.status.setText("В отфильтрованном архиве нет двухтоновых измерений.")
            return
        points = []
        signatures = set()
        for item in fundamentals:
            result = analyze_two_tone(item)
            if result["status"] != "preliminary":
                continue
            result["uncertainty"] = item.get("result", {}).get("uncertainty") or result_uncertainty(item, result)
            plan = item["context"]["two_tone_plan"]
            fixed = {key: value for key, value in plan.items()
                if key not in ("level1_dbm", "level2_dbm", "f1_hz", "lo_hz", "lo_dbm")}
            fixed["level_difference"] = plan["level2_dbm"] - plan["level1_dbm"]
            fixed["if_offset_hz"] = plan["lo_hz"] - plan["f1_hz"] if plan["mode"] == "mixer" else None
            fixed["demo"] = item["demo"]
            fixed["sample"] = item["context"].get("sample")
            fixed["series_id"] = item["context"].get("series_id")
            fixed["source_layout"] = item["context"].get("source_layout", "legacy_smu_akip_tones")
            signatures.add(json.dumps(fixed, sort_keys=True))
            harmonics = [row["context"]["harmonic_result"] for row in self.bench.spectra
                         if row.get("context", {}).get("parent_spectrum_id") == item["id"]]
            points.append(dict(spectrum_id=item["id"], plan=plan, result=result, harmonics=harmonics, demo=item["demo"]))
        if len(signatures) > 1:
            series = {}
            for point in points:
                source = next(item for item in fundamentals if item["id"] == point["spectrum_id"])
                context = source["context"]
                key = context.get("series_id", source["id"])
                label = f"{source['measured_at'][:19]} · {context.get('sample', '')} · {context.get('notes', '')[:100]} · {key}"
                series.setdefault(key, dict(label=label, points=[]))["points"].append(point)
            labels = [item["label"] for item in series.values()]
            label, accepted = QtWidgets.QInputDialog.getItem(self, "Двухтоновая серия", "Выберите серию из отфильтрованного архива", labels, 0, False)
            if not accepted:
                return
            points = next(item["points"] for item in series.values() if item["label"] == label)
        self.points = sorted(points, key=lambda point: (point["plan"]["f1_hz"], point["plan"]["lo_dbm"],
            point["plan"]["level1_dbm"]))
        self.plot_distortion()
        self.summary.setText(f"Из архива: {len(self.points)} двухтоновых точек, "
            f"частот {len({point['plan']['f1_hz'] for point in self.points})}. Связанные гармонические полосы добавлены по UUID.")
        self.status.setText("Архивные данные; приборы не включались. Вердикт не задаётся.")

    def export_png(self):
        if not self.points:
            self.status.setText("Сначала выполните измерение или загрузите точки из архива.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "График двухтоновых измерений", "two-tone.png", "PNG (*.png)")
        if path:
            try:
                exporter = pg.exporters.ImageExporter(self.plot.plotItem)
                exporter.parameters()["width"] = 2400
                exporter.export(path)
            except Exception as error:
                self.status.setText("Ошибка PNG: " + str(error))

    def export_csv(self):
        if not self.points:
            self.status.setText("Сначала выполните измерение.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Двухтоновые точки", "two-tone.csv", "CSV (*.csv)")
        if path:
            try:
                with open(path, "w", encoding="utf-8-sig", newline="") as stream:
                    writer = csv.writer(stream, delimiter=";")
                    writer.writerow(columns())
                    for point in self.results.rows:
                        writer.writerow(row_values(point))
            except OSError as error:
                self.status.setText("Ошибка CSV: " + str(error))
