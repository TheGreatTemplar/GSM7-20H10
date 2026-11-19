"""Embedded calibration procedure and profile selection for two-tone drive."""

from html import escape

from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg
from shiboken6 import isValid

from .source_calibration import CalibrationPlan, load_profiles
from .rf_storage import SpectrumStore
from .form_layout import compact_inputs, GroupedFormRows
from .validation import FieldError, FieldFeedback, StatusLabel
from .rf_labels import INPUT_ATTENUATION_A, INPUT_ATTENUATION_B, INPUT_ATTENUATION_HINT, AFTER_COMBINER_ATTENUATION
from .plot_hover import PointHover
from .table_filters import configure_table


class PowerCalibrationPanel(QtWidgets.QWidget):
    changed = QtCore.Signal()

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.fields = {}
        self.display_profiles = {}
        self.display_batch = None
        self.plot_curves = {}
        self.curve_signatures = {}
        self.table_signatures = []
        layout = QtWidgets.QVBoxLayout(self)
        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(False)
        layout.addWidget(self.splitter, 1)
        settings_scroll = QtWidgets.QScrollArea()
        self.settings_scroll = settings_scroll
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setMinimumHeight(160)
        settings_widget = QtWidgets.QWidget()
        settings_layout = QtWidgets.QVBoxLayout(settings_widget)
        settings_scroll.setWidget(settings_widget)
        self.splitter.addWidget(settings_scroll)
        instructions = (
            "1. При RF OFF сохраните кабели и аттенюаторы обоих каналов SMU до сумматора.\n"
            "2. Вместо входа DUT подключите выход сумматора к RSA или NRP18T. LO не подключать.\n"
            "3. Здесь задайте диапазон и шаг частоты, ослабления A/B и ряд мощностей на выходе комбинера.\n"
            "RSA измеряет два тона одновременно, поэтому для него задаётся интервал. "
            "NRP18T измеряет A и B по очереди на одной частоте, второй RF-выход выключен.\n"
            "После калибровки верните DUT; кабели, сумматор и аттенюаторы вводов должны остаться прежними.")
        self.instructions_button = QtWidgets.QToolButton()
        self.instructions_button.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.instructions_button.setText("Схема подключения")
        self.instructions_button.setIcon(self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_MessageBoxInformation))
        explanation = "<p>" + escape(instructions).replace("\n", "<br>") + "</p>"
        self.instructions_button.setToolTip(explanation)
        self.instructions_button.clicked.connect(lambda: QtWidgets.QWhatsThis.showText(
            self.instructions_button.mapToGlobal(QtCore.QPoint(0, self.instructions_button.height())),
            explanation, self.instructions_button))
        settings_layout.addWidget(self.instructions_button, alignment=QtCore.Qt.AlignmentFlag.AlignLeft)
        self.settings_columns = QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.Direction.LeftToRight)
        self.settings_columns.setSpacing(8)
        settings_layout.addLayout(self.settings_columns)
        columns = []
        for _ in range(2):
            column = QtWidgets.QWidget()
            column_layout = QtWidgets.QVBoxLayout(column)
            column_layout.setContentsMargins(0, 0, 0, 0)
            column_layout.setSpacing(6)
            columns.append(column_layout)
            self.settings_columns.addWidget(column, 1)
        self.form = GroupedFormRows()
        forms = {}
        self.setting_groups = {}
        for key, title, side in (("meter", "Измеритель", 0), ("path", "Тракт / ослабления", 0),
                ("frequency", "Частоты", 1), ("power", "Мощность на выходе комбинера", 1),
                ("precision", "Точность и повторы", 1)):
            group = QtWidgets.QGroupBox(title)
            forms[key] = QtWidgets.QFormLayout(group)
            forms[key].setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
            forms[key].setVerticalSpacing(4)
            self.setting_groups[key] = group
            columns[side].addWidget(group)
        for column in columns:
            column.addStretch()
        settings_scroll.viewport().installEventFilter(self)
        categories = {"method": "meter", "nrp_resource": "meter", "nrp_averages": "meter", "nrp_settling_s": "meter",
            "path": "path", "loss1": "path", "loss2": "path", "measurement_loss": "path",
            "frequency_start": "frequency", "frequency_stop": "frequency", "frequency_step": "frequency", "spacing": "frequency",
            "first": "power", "last": "power", "step": "power", "tolerance": "precision",
            "max_correction": "precision", "samples": "precision"}
        captions = {"nrp_resource": "USB-ресурс", "nrp_averages": "Усреднение, отсч.", "nrp_settling_s": "Пауза, с",
            "path": "Название", "loss1": INPUT_ATTENUATION_A, "loss2": INPUT_ATTENUATION_B,
            "measurement_loss": AFTER_COMBINER_ATTENUATION, "frequency_start": "Начало, МГц", "frequency_stop": "Конец, МГц",
            "frequency_step": "Шаг, МГц", "spacing": "Интервал A/B, Гц", "first": "Начало, dBm", "last": "Конец, dBm",
            "step": "Шаг, дБ", "tolerance": "Допуск, дБ", "max_correction": "Предел поправки, дБ", "samples": "Показаний на итерацию"}

        def add(key, title, widget):
            self.fields[key] = widget
            form = forms[categories[key]]
            form.addRow(captions.get(key, title), widget)
            widget.setToolTip(title)
            self.form.register(widget, form)
            return widget

        def number(value, low, high, decimals=2):
            widget = QtWidgets.QDoubleSpinBox()
            widget.setRange(low, high)
            widget.setDecimals(decimals)
            widget.setValue(value)
            return widget

        self.method = add("method", "Измеритель", QtWidgets.QComboBox())
        self.method.addItem("RSA6265", "rsa")
        self.method.addItem("NRP18T · USB", "nrp")
        self.method.setToolTip("RSA: оба тона одновременно. NRP18T: абсолютная мощность каждого канала по очереди.")
        add("nrp_resource", "NRP18T · VISA USB", QtWidgets.QLineEdit("USB0::0x0AAD::0x0150::103694::INSTR"))
        averaging = add("nrp_averages", "Усреднение NRP18T, отсчётов", QtWidgets.QComboBox())
        for value in (16, 32, 64, 128, 256, 512):
            averaging.addItem(str(value), value)
        averaging.setCurrentIndex(averaging.findData(128))
        averaging.setToolTip("Внутреннее усреднение датчика мощности. Больше отсчётов — меньше разброс, дольше измерение. "
            "Число повторных показаний на итерацию задаётся отдельно.")
        add("nrp_settling_s", "Пауза NRP после смены уровня / канала, с", number(1, 0, 30))
        add("path", "Название тракта / комплекта кабелей", QtWidgets.QLineEdit("SMU A+B → сумматор"))
        first_frequency = owner.fields["f1_mhz"].value()
        add("frequency_start", "Начальная частота первого тона, МГц", number(first_frequency, 0.1, 200, 6))
        add("frequency_stop", "Конечная частота первого тона, МГц", number(first_frequency, 0.1, 200, 6))
        add("frequency_step", "Шаг частоты, МГц", number(1, 0.000001, 200, 6))
        add("spacing", "Интервал между тонами A и B, Гц", number(owner.fields["spacing_hz"].value(), 100, 100000, 0))
        add("loss1", INPUT_ATTENUATION_A, number(owner.fields["loss1"].value(), 0, 100))
        add("loss2", INPUT_ATTENUATION_B, number(owner.fields["loss2"].value(), 0, 100))
        for key, channel in (("loss1", "A/1"), ("loss2", "B/2")):
            self.fields[key].setToolTip(INPUT_ATTENUATION_HINT.format(channel=channel))
        add("first", "Начальная мощность тона на выходе комбинера, dBm", number(-26, -110, 20))
        add("last", "Конечная мощность тона на выходе комбинера, dBm", number(-16, -110, 20))
        add("step", "Шаг уровня, дБ", number(1, 0.1, 20))
        add("tolerance", "Допуск уровня и разности тонов, дБ", number(0.1, 0.02, 0.5))
        add("max_correction", "Предел поправки канала, дБ", number(3, 0.1, 10))
        add("measurement_loss", "Ослабление от выхода комбинера до измерителя, дБ", number(0, 0, 100))
        self.fields["measurement_loss"].setToolTip("Только дополнительный кабель/аттенюатор после комбинера. "
            "Если NRP подключён непосредственно к выходу комбинера — 0 дБ. Ослабления A/B здесь повторно не учитываются.")
        add("samples", "Измерений на итерацию", number(5, 1, 20, 0))
        self.use_levels = QtWidgets.QPushButton("Взять частоты и уровни из двухтонового режима")
        self.use_levels.clicked.connect(self.copy_levels)
        settings_layout.addWidget(self.use_levels)
        self.use_levels.setSizePolicy(QtWidgets.QSizePolicy.Policy.Maximum, QtWidgets.QSizePolicy.Policy.Fixed)
        settings_layout.addStretch()
        self.plan_summary = StatusLabel()
        layout.addWidget(self.plan_summary)
        compact_inputs(settings_widget)
        self.confirm = QtWidgets.QCheckBox("Измеритель подключён вместо DUT; схема проверена")
        self.confirm.setToolTip("DUT отключён; кабели и ослабления проверены; сигнал LO не попадает в измеритель.")
        self.confirm.setStyleSheet("QCheckBox { font-weight: bold; padding: 5px; }"
            "QCheckBox::indicator { width: 20px; height: 20px; }"
            "QCheckBox::indicator:unchecked { border: 2px solid #c59b51; border-radius: 3px; background: #243444; }")
        layout.addWidget(self.confirm)
        buttons = QtWidgets.QHBoxLayout()
        self.start_button = QtWidgets.QPushButton("Выполнить калибровку уровней")
        self.start_button.clicked.connect(lambda: owner.launch("calibrate"))
        buttons.addWidget(self.start_button)
        stop = QtWidgets.QPushButton("СТОП / RF OFF")
        stop.clicked.connect(owner.stop)
        buttons.addWidget(stop)
        layout.addLayout(buttons)
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.setMinimumHeight(130)
        self.plot.setLabel("bottom", "Мощность каждого тона на выходе комбинера", units="dBm")
        self.plot.setLabel("left", "Поправка задания SMU", units="dB")
        self.point_hover = PointHover(self.plot, self.point_hint_text)
        results = QtWidgets.QTabWidget()
        self.results_tabs = results
        results.setMinimumHeight(130)
        self.reset_scale_button = QtWidgets.QPushButton("Сбросить масштаб")
        self.reset_scale_button.clicked.connect(self.reset_scale)
        plot_tools = QtWidgets.QWidget()
        plot_tools_layout = QtWidgets.QHBoxLayout(plot_tools)
        plot_tools_layout.setContentsMargins(0, 0, 0, 0)
        self.frequency_filter = QtWidgets.QComboBox()
        self.frequency_filter.setMaximumWidth(180)
        self.frequency_filter.addItem("Все частоты", "all")
        self.frequency_filter.setToolTip("Показать все частоты серии или одну частоту на графике и в таблице")
        self.frequency_filter.currentIndexChanged.connect(self.redraw_series)
        plot_tools_layout.addWidget(self.frequency_filter)
        plot_tools_layout.addWidget(self.reset_scale_button)
        results.setCornerWidget(plot_tools, QtCore.Qt.Corner.TopRightCorner)
        results.addTab(self.plot, "Поправки каналов")
        self.table = QtWidgets.QTableWidget(0, 11)
        configure_table(self.table)
        self.table.setHorizontalHeaderLabels(["Частота,\nМГц", "Цель,\ndBm", "SMU A,\ndBm", "SMU B,\ndBm",
            "Изм. A,\ndBm", "Изм. B,\ndBm", "A−B, дБ", "U A/1,\ndB k=2", "U B/2,\ndB k=2",
            "U A−B,\ndB k=2", "Результат"])
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setDefaultSectionSize(38)
        results.addTab(self.table, "Контроль уровней")
        self.splitter.addWidget(results)
        self.splitter_sizes = [500, 180]
        self.splitter.setSizes(self.splitter_sizes)
        self.splitter.splitterMoved.connect(self.splitter_moved)
        profiles = QtWidgets.QHBoxLayout()
        self.use_profile = QtWidgets.QCheckBox("Применять калибровку в двухтоновых измерениях")
        layout.addWidget(self.use_profile)
        self.profiles = QtWidgets.QComboBox()
        self.profiles.setMinimumContentsLength(20)
        self.profiles.setMaximumWidth(520)
        self.profiles.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        profiles.addWidget(self.profiles, 1)
        reload_button = QtWidgets.QPushButton("Обновить профили")
        reload_button.clicked.connect(self.reload)
        profiles.addWidget(reload_button)
        profiles.addStretch()
        layout.addLayout(profiles)
        self.status = StatusLabel("Профиль действует только на измеренных частотах и уровнях; интерполяции нет.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.profiles.currentIndexChanged.connect(self.profile_selected)
        self.use_profile.toggled.connect(lambda: self.changed.emit())
        self.method.currentIndexChanged.connect(self.method_changed)
        mapped = dict(owner.bench.fields, **self.fields)
        mapped.update(first_dbm=self.fields["first"], last_dbm=self.fields["last"], step_db=self.fields["step"],
            f1_hz=self.fields["frequency_start"], stop_frequency_hz=self.fields["frequency_stop"],
            frequency_step_hz=self.fields["frequency_step"], spacing_hz=self.fields["spacing"],
            loss1_db=self.fields["loss1"], loss2_db=self.fields["loss2"], tolerance_db=self.fields["tolerance"],
            max_correction_db=self.fields["max_correction"], measurement_loss_db=self.fields["measurement_loss"],
            profiles=self.profiles, confirm=self.confirm)
        self.feedback = FieldFeedback(mapped)
        for widget in self.fields.values():
            signal = widget.currentIndexChanged if isinstance(widget, QtWidgets.QComboBox) else (
                widget.valueChanged if isinstance(widget, QtWidgets.QDoubleSpinBox) else widget.textChanged)
            signal.connect(self.settings_changed)
        self.method_changed()
        self.update_plan_summary()

    def method_changed(self, *args):
        nrp = self.method.currentData() == "nrp"
        self.form.setRowVisible(self.fields["spacing"], not nrp)
        self.fields["spacing"].setEnabled(not nrp)
        for key in ("nrp_resource", "nrp_averages", "nrp_settling_s"):
            self.form.setRowVisible(self.fields[key], nrp)
            self.fields[key].setEnabled(nrp)
        self.setting_groups["frequency"].setTitle("Частоты каналов A/B" if nrp else "Частоты первого тона")

    def eventFilter(self, watched, event):
        if not isValid(self.settings_scroll):
            return False
        if watched is self.settings_scroll.viewport() and event.type() == QtCore.QEvent.Type.Resize:
            direction = (QtWidgets.QBoxLayout.Direction.LeftToRight if event.size().width() >= 720 else
                QtWidgets.QBoxLayout.Direction.TopToBottom)
            self.settings_columns.setDirection(direction)
        return super().eventFilter(watched, event)

    def reset_scale(self):
        self.plot.autoRange()
        self.plot.enableAutoRange(x=True, y=True)

    def splitter_moved(self, *args):
        self.splitter_sizes = self.splitter.sizes()
        self.changed.emit()

    def copy_levels(self):
        settings = self.owner.settings()
        target = settings["level1"] - settings["loss1"]
        if abs(target - (settings["level2"] - settings["loss2"])) > 1e-6:
            self.status.setText("Для переноса уровней задайте одинаковую номинальную мощность двух тонов после ослаблений.")
            return
        self.fields["first"].setValue(target)
        self.fields["last"].setValue(settings["drive_stop"] - settings["loss1"] if settings["sweep"] else target)
        self.fields["step"].setValue(settings["drive_step"])
        self.fields["frequency_start"].setValue(settings["f1_mhz"])
        self.fields["frequency_stop"].setValue(settings["f1_mhz"])
        self.fields["spacing"].setValue(settings["spacing_hz"])
        self.status.setText("Уровни перенесены. Проверьте диапазон и подключение, затем запустите калибровку.")

    def settings_changed(self, *args):
        self.confirm.setChecked(False)
        self.update_plan_summary()
        self.changed.emit()

    def update_plan_summary(self):
        if not hasattr(self, "feedback"):
            return
        previously_invalid = getattr(self, "parameters_invalid", False)
        self.parameters_invalid = False
        self.feedback.clear()
        try:
            plan = self.plan()
            count = len(plan.frequency_pairs())
            levels = len(plan.levels())
            self.plan_summary.setText(f"План: {count} частот × {levels} уровней = {count * levels} двухканальных точек. "
                f"Отдельных точек A/B: {count * levels * 2}.")
            if previously_invalid:
                self.status.setText("Параметры исправлены. " + self.plan_summary.text())
        except ValueError as error:
            self.parameters_invalid = True
            self.plan_summary.setText("ОШИБКА ПЛАНА: проверьте выделенные поля.")
            self.status.setText("ОШИБКА ПАРАМЕТРОВ: " + str(error))
            self.feedback.show(error, reveal=False)

    def error(self, error):
        self.status.setText("ОШИБКА: " + str(error))
        self.feedback.show(error)

    def prepare_run(self):
        self.feedback.clear()
        self.plot.clear()
        self.point_hover.restore_lines()
        self.plot_curves.clear()
        self.curve_signatures.clear()
        self.table_signatures.clear()
        self.table.setRowCount(0)
        self.current_profile = None
        self.display_profiles.clear()
        self.display_batch = None
        self.status.setText("Начинается новая серия; предыдущий график очищен.")

    def settings(self):
        result = {key: widget.currentData() if isinstance(widget, QtWidgets.QComboBox) else
            widget.value() if isinstance(widget, QtWidgets.QDoubleSpinBox) else widget.text().strip()
            for key, widget in self.fields.items()}
        result.update(use=self.use_profile.isChecked(), profile_id=self.profiles.currentData(),
            splitter_sizes=self.splitter_sizes)
        return result

    def restore(self, data):
        for key in ("frequency_start", "frequency_stop"):
            if key not in data:
                self.fields[key].setValue(self.owner.fields["f1_mhz"].value())
        if "spacing" not in data:
            self.fields["spacing"].setValue(self.owner.fields["spacing_hz"].value())
        for key in ("loss1", "loss2"):
            if key not in data:
                self.fields[key].setValue(self.owner.fields[key].value())
        for key, value in data.items():
            if key not in self.fields:
                continue
            widget = self.fields[key]
            if isinstance(widget, QtWidgets.QComboBox):
                widget.setCurrentIndex(max(0, widget.findData(value)))
            elif isinstance(widget, QtWidgets.QDoubleSpinBox):
                widget.setValue(value)
            else:
                widget.setText(str(value))
        self.reload()
        self.profiles.setCurrentIndex(self.profiles.findData(data.get("profile_id")))
        self.use_profile.setChecked(bool(data.get("use", False)))
        sizes = data.get("splitter_sizes")
        if isinstance(sizes, list) and len(sizes) == 2 and all(isinstance(value, int) and value > 0 for value in sizes):
            self.splitter_sizes = list(sizes)
            self.splitter.setSizes(sizes)
        self.update_plan_summary()

    def plan(self):
        data = self.settings()
        plan = CalibrationPlan(f1_hz=data["frequency_start"] * 1e6,
            f2_hz=data["frequency_start"] * 1e6 + (data["spacing"] if data["method"] == "rsa" else 0),
            loss1_db=data["loss1"], loss2_db=data["loss2"],
            first_dbm=data["first"], last_dbm=data["last"], step_db=data["step"], tolerance_db=data["tolerance"],
            max_correction_db=data["max_correction"], measurement_loss_db=data["measurement_loss"],
            samples=int(data["samples"]), method=data["method"], stop_frequency_hz=data["frequency_stop"] * 1e6,
            frequency_step_hz=data["frequency_step"] * 1e6)
        plan.levels()
        return plan

    def reload(self):
        selected = self.profiles.currentData()
        self.archived = load_profiles(self.owner.bench.root, self.owner.bench.demo.isChecked(), include_incomplete=True)
        try:
            database = SpectrumStore(self.owner.bench.store().path, self.owner.bench.demo.isChecked())
            database_profiles = database.load_calibrations()
            known = {item["id"] for item in database_profiles}
            for profile in self.archived:
                if profile["id"] not in known:
                    database.save_calibration(profile)
                    database_profiles.append(profile)
            known = {item["id"] for item in self.archived}
            self.archived.extend(item for item in database_profiles if item["id"] not in known)
        except Exception as error:
            self.owner.bench.log.appendPlainText("Не удалось добавить профили калибровки в БД смесителей: " + str(error))
        self.available = [item for item in self.archived if item.get("state") in ("complete", "complete_with_gaps")
            and item.get("rf_off_confirmed") and item.get("points")]
        blocker = QtCore.QSignalBlocker(self.profiles)
        self.profiles.clear()
        for profile in self.available:
            plan = profile["plan"]
            self.profiles.addItem(f"{profile['created_at'][:16]} · {plan['method'].upper()} · "
                f"{plan['f1_hz'] / 1e6:g}/{plan['f2_hz'] / 1e6:g} МГц · "
                f"{plan['first_dbm']:+g}…{plan['last_dbm']:+g} dBm · {profile['path']}", profile["id"])
            self.profiles.setItemData(self.profiles.count() - 1, self.profiles.itemText(self.profiles.count() - 1),
                QtCore.Qt.ItemDataRole.ToolTipRole)
        if selected:
            self.profiles.setCurrentIndex(self.profiles.findData(selected))
        del blocker
        self.profile_selected()

    def selected_profile(self):
        profile = next((item for item in getattr(self, "available", []) if item["id"] == self.profiles.currentData()), None)
        if profile is None:
            raise FieldError("Выберите завершённый профиль в разделе «Калибровка уровней SMU»", "profiles")
        return profile

    def profile_for_plan(self, plan):
        selected = self.selected_profile()
        batch = selected.get("calibration_batch_id", selected["id"])
        for profile in self.available:
            if (profile.get("calibration_batch_id", profile["id"]) == batch
                    and abs(profile["plan"]["f1_hz"] - plan.f1_hz) <= 0.05
                    and (abs(profile["plan"]["f2_hz"] - plan.f2_hz) <= 0.05
                        or (profile["plan"]["method"] == "nrp" and profile["plan"]["f1_hz"] == profile["plan"]["f2_hz"]))):
                return profile
        raise FieldError("В выбранной серии нет калибровки для этой пары частот", "profiles")

    def profile_selected(self, *args):
        if self.profiles.currentIndex() >= 0:
            self.received(self.selected_profile())
        self.changed.emit()

    def received(self, profile):
        self.current_profile = profile
        batch = profile.get("calibration_batch_id", profile["id"])
        if batch != self.display_batch:
            self.display_batch = batch
            self.display_profiles = {item["id"]: item for item in getattr(self, "archived", [])
                if item.get("calibration_batch_id", item["id"]) == batch}
            self.frequency_filter.setCurrentIndex(0)
        self.display_profiles[profile["id"]] = profile
        selected = self.frequency_filter.currentData()
        blocker = QtCore.QSignalBlocker(self.frequency_filter)
        self.frequency_filter.clear()
        self.frequency_filter.addItem("Все частоты", "all")
        for item in sorted(self.display_profiles.values(), key=lambda value: value["plan"]["f1_hz"]):
            plan = item["plan"]
            key = f"{plan['f1_hz']:.12g}:{plan['f2_hz']:.12g}"
            if self.frequency_filter.findData(key) < 0:
                first = f"{plan['f1_hz'] / 1e6:.6f}".rstrip("0").rstrip(".")
                second = f"{plan['f2_hz'] / 1e6:.6f}".rstrip("0").rstrip(".")
                label = first + " МГц" if first == second else first + "/" + second + " МГц"
                self.frequency_filter.addItem(label, key)
        self.frequency_filter.setCurrentIndex(max(0, self.frequency_filter.findData(selected)))
        del blocker
        self.redraw_series()
        points = profile["points"]
        state = {"running": "Измерение", "complete": "Завершено", "complete_with_gaps": "Завершено с пропусками",
            "cancelled": "Остановлено", "failed": "ОШИБКА"}.get(profile["state"], profile["state"])
        limits = sum(self.failure_kind(item) == "source_limit" for item in profile.get("skipped", []))
        self.status.setText(f"{state} · {profile['plan']['f1_hz'] / 1e6:g} МГц · "
            f"уровней {len(points)}, пропущено {len(profile.get('skipped', []))} (предел SMU: {limits}) · "
            f"{profile.get('meter_identity', '')} · RF OFF: {'да' if profile['rf_off_confirmed'] else 'не подтверждён'}"
            + (" · ОШИБКА: " + profile["error"] if profile.get("error") else "")
            + (" · " + profile["off_error"] if profile.get("off_error") else ""))

    @staticmethod
    def failure_kind(point):
        if point.get("kind"):
            return point["kind"]
        reason = point.get("error", "")
        if "SMU" in reason and any(word in reason for word in ("+20", "предел")):
            return "source_limit"
        if "сошлись" in reason:
            return "nonconvergence"
        if "Поправка" in reason:
            return "correction_limit"
        return "measurement_error"

    def redraw_series(self, *args):
        if not hasattr(self, "table"):
            return
        selection = self.frequency_filter.currentData()
        profiles = sorted((item for item in self.display_profiles.values()
            if selection == "all" or selection == f"{item['plan']['f1_hz']:.12g}:{item['plan']['f2_hz']:.12g}"),
            key=lambda item: (item["plan"]["f1_hz"], item["plan"]["f2_hz"]))
        legend = self.plot.addLegend()
        legend.setVisible(len(profiles) <= 10)
        count = sum(len(item["points"]) for item in profiles)
        skipped = sum(len(item.get("skipped", [])) for item in profiles)
        self.plot.setTitle(f"Частот: {len(profiles)} · принято {count}, пропущено {skipped}")
        rows = [(item["plan"], point, item["state"]) for item in profiles
            for point in sorted(item["points"] + item.get("skipped", []), key=lambda value: value["target_dbm"])]
        self.table.setRowCount(len(rows))
        signatures = []
        for row, (plan, point, state) in enumerate(rows):
            failed = "error" in point
            frequency = f"{plan['f1_hz'] / 1e6:.6f}" if plan["f1_hz"] == plan["f2_hz"] else f"{plan['f1_hz'] / 1e6:.6f}\n{plan['f2_hz'] / 1e6:.6f}"
            outcome = {"source_limit": "Предел SMU", "nonconvergence": "Не сошлось", "correction_limit": "Поправка > лимита",
                "measurement_error": "Ошибка измерения"}.get(self.failure_kind(point), "Пропуск") if failed else "Измерено"
            if not failed and state in ("cancelled", "failed"):
                outcome = "Измерено*"
            values = [point["target_dbm"], *point["commands_dbm"], *point.get("measured_dbm", [None, None]), point.get("imbalance_db")]
            channels = point.get("uncertainty", {}).get("channels", [])
            uncertainty_values = [channels[index].get("expanded_combined_uncertainty_k2_db")
                if index < len(channels) else None for index in (0, 1)]
            uncertainty_values.append(point.get("uncertainty", {}).get("imbalance_a_minus_b", {}).get("expanded_uncertainty_k2_db"))
            texts = ([frequency] + [f"{value:+.3f}" if value is not None else "—" for value in values]
                + [f"{value:.3f}" if value is not None else "—" for value in uncertainty_values] + [outcome])
            signature = (tuple(texts), point.get("error"), state in ("cancelled", "failed"), self.failure_kind(point) if failed else None)
            signatures.append(signature)
            if row < len(self.table_signatures) and self.table_signatures[row] == signature:
                continue
            for column, value in enumerate(texts):
                item = QtWidgets.QTableWidgetItem(value)
                if state in ("cancelled", "failed"):
                    item.setToolTip("Частотный профиль не завершён. Точка доступна для просмотра, но профиль не применяется к генератору.")
                if not failed and column in (4, 5):
                    channel = column - 4
                    uncertainty = point.get("uncertainty", {})
                    components = uncertainty.get("channels", [])
                    if channel < len(components):
                        component = components[channel]
                        if component.get("standard_uncertainty_db") is not None:
                            analyzer = uncertainty.get("analyzer", {})
                            analyzer_channels = analyzer.get("channels", [])
                            analyzer_u = analyzer_channels[channel].get("standard_uncertainty_db") if channel < len(analyzer_channels) else None
                            combined_u = component.get("expanded_combined_uncertainty_k2_db")
                            analyzer_u_text = f"{analyzer_u:.4f} дБ" if analyzer_u is not None else "не указана"
                            combined_u_text = f"{combined_u:.4f} дБ" if combined_u is not None else "не рассчитана"
                            item.setToolTip(
                                f"Стандартная неопределённость повторяемости uA: {component['standard_uncertainty_db']:.4f} дБ\n"
                                f"Расширенная повторяемость UА (k=2): {component['expanded_uncertainty_k2_db']:.4f} дБ\n"
                                f"Неопределённость RSA (по РЭ): {analyzer_u_text}\n"
                                f"Комбинированная расширенная U (k=2): {combined_u_text}\n"
                                "Оценка по паспортным пределам; фактические настройки RSA отличаются.")
                if failed:
                    item.setBackground(QtGui.QColor("#665027" if self.failure_kind(point) == "source_limit" else "#612c34"))
                    item.setForeground(QtGui.QColor("#ffffff"))
                    item.setToolTip(point["error"])
                self.table.setItem(row, column, item)
        self.table_signatures = signatures
        visible = {(profile["id"], channel) for profile in profiles for channel in (0, 1)}
        for key in list(self.plot_curves):
            if key not in visible:
                self.point_hover.forget(self.plot_curves[key])
                self.plot.removeItem(self.plot_curves.pop(key))
                self.curve_signatures.pop(key, None)
        for index, profile in enumerate(profiles):
            points = sorted(profile["points"], key=lambda point: point["target_dbm"])
            color = pg.intColor(int(round(profile["plan"]["f1_hz"])) % 251, hues=251, values=1, maxHue=300, minValue=220)
            for channel in (0, 1):
                hz = profile["plan"][("f1_hz", "f2_hz")[channel]]
                label = f"{hz / 1e6:.6f}".rstrip("0").rstrip(".")
                key = (profile["id"], channel)
                signature = tuple((point["target_dbm"], point["correction_db"][channel]) for point in points)
                if key not in self.plot_curves:
                    self.plot_curves[key] = self.plot.plot(
                        pen=pg.mkPen(color, width=2, style=QtCore.Qt.PenStyle.SolidLine if channel == 0 else QtCore.Qt.PenStyle.DashLine),
                        symbol="o" if channel == 0 else "t", name=f"{label} МГц · {('A/1', 'B/2')[channel]}")
                if self.curve_signatures.get(key) != signature:
                    x, y = [value[0] for value in signature], [value[1] for value in signature]
                    data = [dict(frequency_hz=hz, channel=channel, index=index + 1, count=len(points),
                        power_dbm=point["target_dbm"], correction_db=point["correction_db"][channel],
                        command_dbm=point["commands_dbm"][channel], measured_dbm=point["measured_dbm"][channel],
                        uncertainty=point.get("uncertainty"))
                        for index, point in enumerate(points)]
                    self.plot_curves[key].setData(x, y, data=data)
                    self.point_hover.attach(self.plot_curves[key], x, y, data)
                    self.curve_signatures[key] = signature

    @staticmethod
    def point_hint_text(point):
        frequency = f"{point['frequency_hz'] / 1e6:.6f}".rstrip("0").rstrip(".")
        text = (f"{frequency} МГц · канал {('A/1', 'B/2')[point['channel']]} · точка {point['index']}/{point['count']}\n"
            f"На выходе комбинера: {point['power_dbm']:+.3f} dBm\n"
            f"Поправка: {point['correction_db']:+.3f} дБ\n"
            f"Задано SMU: {point['command_dbm']:+.3f} dBm\n"
            f"Измерено: {point['measured_dbm']:+.3f} dBm")
        uncertainty = point.get("uncertainty", {})
        channels = uncertainty.get("channels", [])
        channel = point["channel"]
        if channel < len(channels) and channels[channel].get("standard_uncertainty_db") is not None:
            component = channels[channel]
            analyzer = uncertainty.get("analyzer", {}).get("channels", [])
            analyzer_component = analyzer[channel] if channel < len(analyzer) else {}
            analyzer_u = analyzer_component.get("standard_uncertainty_db")
            combined_u = component.get("expanded_combined_uncertainty_k2_db")
            analyzer_u_text = f"{analyzer_u:.4f} дБ" if analyzer_u is not None else "не указана"
            combined_u_text = f"{combined_u:.4f} дБ" if combined_u is not None else "не рассчитана"
            text += (f"\nuA повторяемости: {component['standard_uncertainty_db']:.4f} дБ"
                f"\nUА (k=2): {component['expanded_uncertainty_k2_db']:.4f} дБ"
                f"\nuB RSA по РЭ: {analyzer_u_text}"
                f"\nUкомб (k=2): {combined_u_text}"
                "\nОценка по РЭ; условия измерения отличаются от паспортных")
        return text

    def set_busy(self, busy):
        for widget in self.fields.values():
            widget.setEnabled(not busy)
        self.confirm.setEnabled(not busy)
        self.start_button.setEnabled(not busy)
        self.use_levels.setEnabled(not busy)
        self.profiles.setEnabled(not busy)
        self.use_profile.setEnabled(not busy)
        if not busy:
            self.method_changed()
