"""Qt desktop workbench with background I/O and a durable recovery queue."""

import csv
from html import escape
from dataclasses import asdict
import json
from pathlib import Path
import threading

from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

from .core import Recipe, generate_levels, match_groups, simulate
from . import __version__
from .instrument import Instrument, SocketTransport, VisaTransport
from .storage import Store, new_run
from .help_ui import AboutDialog, HelpDialog


ROOT = Path(__file__).resolve().parent.parent
SETTINGS = ROOT / "settings.local.json"
ICON_PATH = Path(__file__).resolve().parent.parent / "assets" / "gsmbench.ico"
STYLE = """
QWidget
{
    background: #111923;
    color: #d8e2ef;
    font-family: 'Segoe UI';
    font-size: 10pt;
}
QGroupBox
{
    border: 1px solid #314055;
    border-radius: 8px;
    margin-top: 14px;
    padding: 12px 8px 8px 8px;
}
QGroupBox::title
{
    color: #8191a8;
    subcontrol-origin: margin;
    left: 12px;
}
QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox, QPlainTextEdit, QTableWidget
{
    background: #192536;
    border: 1px solid #314055;
    padding: 5px;
    selection-background-color: #305d70;
}
QPushButton
{
    background: #26384c;
    border: 1px solid #405570;
    border-radius: 5px;
    padding: 8px;
}
QPushButton:hover
{
    background: #35506a;
}
QPushButton:disabled
{
    color: #607087;
}
QPushButton#start
{
    background: #17645f;
    color: white;
}
QPushButton#stop
{
    background: #773d43;
    color: white;
}
QHeaderView::section
{
    background: #223247;
    padding: 6px;
    border: 0px;
}
QTabBar::tab
{
    padding: 9px 10px;
    background: #182333;
    color: #a5b4c8;
    border: 1px solid #3b5068;
    border-top: 3px solid #3b5068;
    border-top-left-radius: 5px;
    border-top-right-radius: 5px;
    margin-right: 3px;
}
QTabBar::tab:selected
{
    color: #ffffff;
    background: #28515b;
    border: 1px solid #45d7d0;
    border-top: 3px solid #45d7d0;
    font-weight: 600;
}
QTabBar::tab:hover:!selected
{
    background: #26394d;
}
QTabWidget::pane
{
    border: 1px solid #3b5068;
    top: 0px;
}
"""


class Worker(QtCore.QThread):
    log = QtCore.Signal(str)
    point = QtCore.Signal(object)
    result = QtCore.Signal(object)
    identified = QtCore.Signal(str)
    failed = QtCore.Signal(str)

    def __init__(self, settings, recipe=None, metadata=None):
        super().__init__()
        self.settings = settings
        self.recipe = recipe
        self.metadata = metadata
        self.stop = threading.Event()

    def run(self):
        transport = None
        run = None
        try:
            demo = self.settings["mode"] == "Демо"
            identity = "SIMULATOR"
            if not demo:
                if self.settings["mode"] == "TCP socket":
                    transport = SocketTransport(self.settings["host"], int(self.settings["port"]), self.log.emit)
                else:
                    transport = VisaTransport(self.settings["resource"], self.log.emit)
                instrument = Instrument(transport)
                identity = instrument.identify()
            self.identified.emit(identity)
            if self.recipe is None:
                return
            run = new_run(*self.metadata, self.recipe, demo, identity)

            def receive(point):
                run["points"].append(asdict(point))
                self.point.emit(point)

            if demo:
                for point in simulate(self.recipe):
                    if self.stop.wait(self.recipe.delay):
                        break
                    if not point.valid:
                        raise ValueError("Демо: достигнуто ограничение источника")
                    receive(point)
            else:
                instrument.measure(self.recipe, self.stop, receive)
            run["state"] = "cancelled" if self.stop.is_set() else "complete"
        except Exception as error:
            if run:
                run["state"] = "failed"
                run["error"] = str(error)
            self.failed.emit(str(error) + ("\nПри потере связи проверьте OUTPUT на приборе."
                                          if transport and isinstance(error, (OSError, TimeoutError)) else ""))
        finally:
            if transport:
                try:
                    transport.close()
                except Exception as error:
                    self.log.emit("Закрытие соединения: " + str(error))
            if run:
                self.result.emit(run)


class Window(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.worker = None
        self.runs = []
        self.points = []
        self.axes_swapped = False
        self.active_store = None
        self.initial_position_set = False
        self.help_dialog = None
        self.about_dialog = None
        self.setWindowIcon(QtGui.QIcon(str(ICON_PATH)))
        self.setWindowTitle(f"GSM7-20H10 · Semiconductor Testbench · v{__version__}")
        self.resize(1380, 900)
        self.setMinimumSize(1100, 720)
        self.setStyleSheet(STYLE)
        self.build_menu()
        self.build()
        self.load_settings()
        self.connect_autosave()
        self.refresh()

    def build_menu(self):
        file_menu = self.menuBar().addMenu("Файл")
        file_menu.addAction("Выход", self.close, "Alt+F4")
        help_menu = self.menuBar().addMenu("Справка")
        self.help_action = help_menu.addAction("Помощь по программе…", self.show_help, "F1")
        help_menu.addSeparator()
        self.about_action = help_menu.addAction("О программе…", self.show_about)

    def show_help(self):
        if self.help_dialog is None:
            self.help_dialog = HelpDialog(self)
        self.help_dialog.show()
        self.help_dialog.raise_()
        self.help_dialog.activateWindow()

    def show_about(self):
        if self.about_dialog is None:
            self.about_dialog = AboutDialog(self)
        self.about_dialog.show()
        self.about_dialog.raise_()
        self.about_dialog.activateWindow()

    def showEvent(self, event):
        super().showEvent(event)
        if not self.initial_position_set:
            self.initial_position_set = True
            screen = QtWidgets.QApplication.screenAt(QtGui.QCursor.pos()) or QtWidgets.QApplication.primaryScreen()
            if screen is not None:
                available = screen.availableGeometry()
                self.setMinimumSize(min(1100, available.width()), min(720, available.height()))
                self.resize(min(self.width(), available.width()), min(self.height(), available.height()))
                frame = self.frameGeometry()
                frame.moveCenter(available.center())
                self.move(frame.topLeft())

    def button(self, label, callback, layout):
        button = QtWidgets.QPushButton(label)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def scroll_page(self, widget):
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setWidget(widget)
        return scroll

    def build(self):
        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        layout = QtWidgets.QVBoxLayout(root)
        layout.setContentsMargins(22, 18, 22, 12)
        heading = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("GSM7-20H10  <span style='color:#8191a8'>SEMICONDUCTOR TESTBENCH</span>")
        title.setStyleSheet("font-size:22px; font-weight:700")
        heading.addWidget(title)
        heading.addStretch()
        self.badge = QtWidgets.QLabel("●  ДЕМО")
        self.badge.setStyleSheet("color:#45d7d0")
        heading.addWidget(self.badge)
        layout.addLayout(heading)
        body = QtWidgets.QHBoxLayout()
        layout.addLayout(body, 1)
        self.controls = QtWidgets.QTabWidget()
        self.controls.setFixedWidth(350)
        body.addWidget(self.controls)
        measure_page = QtWidgets.QWidget()
        measure_layout = QtWidgets.QVBoxLayout(measure_page)
        self.controls.addTab(self.scroll_page(measure_page), "Измерение")
        settings_page = QtWidgets.QWidget()
        settings_layout = QtWidgets.QVBoxLayout(settings_page)
        self.controls.addTab(self.scroll_page(settings_page), "Связь / БД")
        connection = QtWidgets.QGroupBox("ETHERNET")
        form = QtWidgets.QFormLayout(connection)
        self.mode = QtWidgets.QComboBox()
        self.mode.addItems(["Демо", "TCP socket", "VISA LAN"])
        self.host = QtWidgets.QLineEdit("172.16.236.32")
        self.port = QtWidgets.QLineEdit("5025")
        self.port.setPlaceholderText("Порт из настроек прибора")
        self.resource = QtWidgets.QLineEdit("TCPIP0::172.16.236.32::5025::SOCKET")
        self.resource.setPlaceholderText("TCPIP0::адрес::…::INSTR")
        self.wire_mode = QtWidgets.QComboBox()
        self.wire_mode.addItem("2 провода", 2)
        self.wire_mode.addItem("4 провода · Kelvin", 4)
        self.terminals = QtWidgets.QComboBox()
        self.terminals.addItem("Передние · FRONT", "front")
        self.terminals.addItem("Задние · REAR", "rear")
        for label, widget in [("Режим", self.mode), ("IP / имя", self.host), ("TCP-порт", self.port),
                              ("VISA ресурс", self.resource)]:
            form.addRow(label, widget)
        form.addRow("Схема", self.wire_mode)
        form.addRow("Клеммы", self.terminals)
        settings_layout.addWidget(connection)
        self.button("Проверить связь · *IDN?", self.identify, settings_layout)
        note = QtWidgets.QLabel("TCP-порт или VISA-ресурс нужно сверить с прибором. Проверка связи не включает выход.")
        note.setWordWrap(True)
        settings_layout.addWidget(note)
        database = QtWidgets.QGroupBox("БАЗА ИЗМЕРЕНИЙ")
        db_layout = QtWidgets.QVBoxLayout(database)
        self.db_path = QtWidgets.QLineEdit(str(ROOT / "data" / "measurements.db"))
        db_layout.addWidget(self.db_path)
        self.button("Выбрать SQLite-базу…", self.choose_db, db_layout)
        self.button("Сохранить настройки ПК", self.save_settings, db_layout)
        self.button("Импорт измерений JSON…", self.import_runs, db_layout)
        self.button("Экспорт базы в JSON…", self.export_runs, db_layout)
        self.button("Восстановить несохранённые…", self.recover, db_layout)
        settings_layout.addWidget(database)
        db_note = QtWidgets.QLabel("Демо хранится отдельно: data/demo.db.\nДля двух ПК переносите JSON измерений. Настройки ПК и БД не входят в Git.")
        db_note.setWordWrap(True)
        settings_layout.addWidget(db_note)
        settings_layout.addStretch()
        specimen = QtWidgets.QGroupBox("ОБРАЗЕЦ")
        form = QtWidgets.QFormLayout(specimen)
        self.kind = QtWidgets.QComboBox()
        self.kind.addItems(["Диод · прямая ВАХ", "J310 · Idss / Vds"])
        self.sample = QtWidgets.QLineEdit("D001")
        self.model = QtWidgets.QLineEdit("КД419")
        self.batch = QtWidgets.QLineEdit("Партия 1")
        for label, widget in [("Методика", self.kind), ("Номер", self.sample), ("Маркировка", self.model), ("Партия", self.batch)]:
            form.addRow(label, widget)
        measure_layout.addWidget(specimen)
        recipe_box = QtWidgets.QGroupBox("УСЛОВИЯ ИЗМЕРЕНИЯ")
        form = QtWidgets.QFormLayout(recipe_box)
        self.spacing = QtWidgets.QComboBox()
        self.spacing.addItem("Логарифмический", "log")
        self.spacing.addItem("Линейный", "linear")
        form.addRow("Ряд", self.spacing)
        bounds = QtWidgets.QWidget()
        bounds_layout = QtWidgets.QHBoxLayout(bounds)
        bounds_layout.setContentsMargins(0, 0, 0, 0)
        self.range_start = QtWidgets.QLineEdit("0.001")
        self.range_stop = QtWidgets.QLineEdit("11")
        bounds_layout.addWidget(self.range_start)
        bounds_layout.addWidget(QtWidgets.QLabel("…"))
        bounds_layout.addWidget(self.range_stop)
        self.range_label = QtWidgets.QLabel("От / до, мА")
        form.addRow(self.range_label, bounds)
        generator = QtWidgets.QWidget()
        generator_layout = QtWidgets.QHBoxLayout(generator)
        generator_layout.setContentsMargins(0, 0, 0, 0)
        self.point_count = QtWidgets.QSpinBox()
        self.point_count.setRange(2, 200)
        self.point_count.setValue(20)
        generator_layout.addWidget(self.point_count)
        self.button("Задать точки", self.generate_points, generator_layout)
        form.addRow("Число точек", generator)
        self.level_label = QtWidgets.QLabel("Токи, мА")
        self.levels = QtWidgets.QLineEdit("0.1; 0.3; 1; 3; 10")
        self.levels.setToolTip("Список точек: разделитель ;. Можно редактировать вручную или создать ряд кнопкой «Задать точки».")
        self.levels.textChanged.connect(self.update_levels_tooltip)
        form.addRow(self.level_label, self.levels)
        self.limit_label = QtWidgets.QLabel("Предел, В")
        self.limit = QtWidgets.QDoubleSpinBox()
        self.limit.setDecimals(3)
        self.limit.setRange(0.001, 100)
        self.limit.setValue(1)
        form.addRow(self.limit_label, self.limit)
        self.delay = QtWidgets.QDoubleSpinBox()
        self.delay.setRange(0.01, 2)
        self.delay.setValue(0.1)
        form.addRow("Задержка, с", self.delay)
        self.temperature = QtWidgets.QDoubleSpinBox()
        self.temperature.setRange(-40, 125)
        self.temperature.setValue(23)
        form.addRow("Температура, °C", self.temperature)
        measure_layout.addWidget(recipe_box)
        self.wiring = QtWidgets.QLabel()
        self.wiring.setWordWrap(True)
        measure_layout.addWidget(self.wiring)
        self.start = self.button("▶  Измерить образец", self.start_measurement, measure_layout)
        self.start.setObjectName("start")
        self.progress = QtWidgets.QProgressBar()
        measure_layout.addWidget(self.progress)
        measure_layout.addStretch()
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        body.addWidget(splitter, 1)
        plot_panel = QtWidgets.QWidget()
        plot_layout = QtWidgets.QVBoxLayout(plot_panel)
        plot_layout.setContentsMargins(0, 0, 0, 0)
        plot_toolbar = QtWidgets.QHBoxLayout()
        plot_layout.addLayout(plot_toolbar)
        plot_toolbar.addStretch()
        self.swap_axes_button = self.button("Поменять оси · X: U / Y: I", self.swap_axes, plot_toolbar)
        pg.setConfigOptions(antialias=True, background="#111923", foreground="#aabbd0")
        self.plot = pg.PlotWidget(title="Вольт-амперные характеристики")
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.setLabel("bottom", "Напряжение", units="V")
        self.plot.setLabel("left", "Ток", units="A")
        self.plot.addLegend()
        plot_layout.addWidget(self.plot)
        splitter.addWidget(plot_panel)
        tabs = QtWidgets.QTabWidget()
        self.result_tabs = tabs
        splitter.addWidget(tabs)
        self.table = QtWidgets.QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(["Образец", "Марка", "Партия", "Метод", "Дата UTC", "Статус", "Точек", "Результат"])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self.plot_selected)
        table_page = QtWidgets.QWidget()
        table_layout = QtWidgets.QVBoxLayout(table_page)
        table_layout.addWidget(self.table)
        row = QtWidgets.QHBoxLayout()
        table_layout.addLayout(row)
        self.button("Обновить", self.refresh, row)
        self.button("CSV выбранных измерений…", self.export_csv, row)
        tabs.addTab(table_page, "Измерения")
        matching_page = QtWidgets.QWidget()
        matching_layout = QtWidgets.QVBoxLayout(matching_page)
        row = QtWidgets.QHBoxLayout()
        matching_layout.addLayout(row)
        self.group_size = QtWidgets.QComboBox()
        self.group_size.addItems(["2", "4"])
        row.addWidget(QtWidgets.QLabel("В группе"))
        row.addWidget(self.group_size)
        self.tolerance = QtWidgets.QDoubleSpinBox()
        self.tolerance.setRange(0.001, 1000)
        self.tolerance.setDecimals(3)
        self.tolerance.setValue(5)
        row.addWidget(QtWidgets.QLabel("Допуск: мВ (диоды) / мА (J310)"))
        row.addWidget(self.tolerance)
        self.button("Подобрать выбранные", self.match, row)
        self.matches = QtWidgets.QPlainTextEdit()
        self.matches.setReadOnly(True)
        matching_layout.addWidget(self.matches)
        tabs.addTab(matching_page, "Подбор пар / четвёрок")
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        tabs.addTab(self.log, "Журнал SCPI")
        splitter.setSizes([410, 330])
        bottom = QtWidgets.QHBoxLayout()
        layout.addLayout(bottom)
        self.status = QtWidgets.QLabel("Готов")
        bottom.addWidget(self.status, 1)
        self.stop_button = self.button("■  СТОП / отключение выхода", self.stop_measurement, bottom)
        self.stop_button.setObjectName("stop")
        self.stop_button.setEnabled(False)
        self.kind.currentIndexChanged.connect(self.change_recipe)
        self.mode.currentIndexChanged.connect(self.refresh)
        self.wire_mode.currentIndexChanged.connect(self.update_wiring)
        self.terminals.currentIndexChanged.connect(self.update_wiring)
        self.change_recipe()

    def change_recipe(self):
        diode = self.kind.currentIndex() == 0
        self.level_label.setText("Токи, мА" if diode else "Vds, В")
        self.limit_label.setText("Предел U, В" if diode else "Предел I, мА")
        self.range_label.setText("От / до, мА" if diode else "От / до, В")
        self.limit.setRange(0.001, 2 if diode else 100)
        self.range_start.setText("0.001" if diode else "0.5")
        self.range_stop.setText("11" if diode else "5")
        self.generate_points()
        self.limit.setValue(1 if diode else 50)
        self.model.setText("КД419" if diode else "J310")
        self.sample.setText("D001" if diode else "Q001")
        self.update_wiring()

    def update_wiring(self):
        terminal = "FRONT" if self.terminals.currentData() == "front" else "REAR"
        diode = self.kind.currentIndex() == 0
        leads = "HI → анод, LO → катод." if diode else "HI → D, LO → S; G соединить с S."
        sense = ""
        if self.wire_mode.currentData() == 4:
            sense = "\nSENSE HI/LO подвести отдельно к тем же выводам компонента."
        self.wiring.setText(f"{terminal}, {self.wire_mode.currentData()} провода: {leads}{sense}")

    def update_levels_tooltip(self):
        unit = "мА" if self.kind.currentIndex() == 0 else "В"
        values = [value.strip() for value in self.levels.text().split(";") if value.strip()]
        # Fixed-width columns keep even a 200-point tooltip inside the screen.
        columns = []
        shown = values[:75]
        for offset in range(0, len(shown), 25):
            column = "<br>".join(f"{index + 1}. {escape(value)} {unit}"
                                 for index, value in enumerate(shown[offset:offset + 25], offset))
            columns.append(f"<td style='padding:4px 10px; vertical-align:top'>{column}</td>")
        remaining = f"<p>Показаны 75 из {len(values)} точек. Полный список — в поле.</p>" if len(values) > 75 else ""
        self.levels.setToolTip("<b>Точки измерения</b><table><tr>" + "".join(columns) + "</tr></table>" + remaining)

    def generate_points(self):
        try:
            start = float(self.range_start.text().strip().replace(",", "."))
            stop = float(self.range_stop.text().strip().replace(",", "."))
            points = generate_levels(start, stop, self.point_count.value(), self.spacing.currentData())
            maximum = 100 if self.kind.currentIndex() == 0 else 10
            if stop > maximum:
                raise ValueError(f"Конец диапазона должен быть ≤ {maximum} в указанных единицах")
            self.levels.setText("; ".join(format(value, ".12g") for value in points))
            self.levels.setCursorPosition(0)
        except Exception as error:
            self.error(str(error))

    def settings(self):
        return dict(mode=self.mode.currentText(), host=self.host.text().strip(), port=self.port.text().strip(),
                    resource=self.resource.text().strip(), database=self.db_path.text().strip(),
                    ui={key: (widget.currentIndex() if isinstance(widget, QtWidgets.QComboBox) else
                              widget.value() if isinstance(widget, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)) else
                              widget.text()) for key, widget in self.setting_widgets().items()},
                    axes_swapped=self.axes_swapped, control_tab=self.controls.currentIndex(),
                    result_tab=self.result_tabs.currentIndex())

    def setting_widgets(self):
        return dict(kind=self.kind, sample=self.sample, model=self.model, batch=self.batch,
                    spacing=self.spacing, range_start=self.range_start, range_stop=self.range_stop,
                    point_count=self.point_count, levels=self.levels, limit=self.limit,
                    delay=self.delay, temperature=self.temperature, wires=self.wire_mode,
                    terminals=self.terminals, group_size=self.group_size, tolerance=self.tolerance)

    def load_settings(self):
        if SETTINGS.exists():
            try:
                data = json.loads(SETTINGS.read_text(encoding="utf-8"))
                for key, widget in [("host", self.host), ("port", self.port), ("resource", self.resource), ("database", self.db_path)]:
                    if key in data and (key != "resource" or str(data[key]).strip()):
                        widget.setText(str(data[key]))
                saved = data.get("ui", {})
                # Apply the method first: changing it initializes the other fields.
                for key, widget in self.setting_widgets().items():
                    if key not in saved:
                        continue
                    value = saved[key]
                    if isinstance(widget, QtWidgets.QComboBox):
                        if type(value) is int and 0 <= value < widget.count():
                            widget.setCurrentIndex(value)
                    elif isinstance(widget, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
                        widget.setValue(value)
                    else:
                        widget.setText(str(value))
                if self.mode.findText(data.get("mode", "Демо")) >= 0:
                    self.mode.setCurrentText(data.get("mode", "Демо"))
                if data.get("axes_swapped", False) != self.axes_swapped:
                    self.swap_axes()
                for key, widget in (("control_tab", self.controls), ("result_tab", self.result_tabs)):
                    index = data.get(key, 0)
                    if type(index) is int and 0 <= index < widget.count():
                        widget.setCurrentIndex(index)
                self.update_wiring()
                self.update_levels_tooltip()
            except Exception as error:
                self.error(str(error))

    def save_settings(self):
        if self.persist_settings():
            self.refresh()
            self.status.setText("Настройки этого ПК сохранены")

    def persist_settings(self):
        try:
            temporary = SETTINGS.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.settings(), ensure_ascii=False, indent=4), encoding="utf-8")
            temporary.replace(SETTINGS)
            return True
        except Exception as error:
            self.error(str(error))
            return False

    def connect_autosave(self):
        self.settings_timer = QtCore.QTimer(self)
        self.settings_timer.setSingleShot(True)
        self.settings_timer.setInterval(500)
        self.settings_timer.timeout.connect(self.persist_settings)
        widgets = list(self.setting_widgets().values()) + [self.mode, self.host, self.port, self.resource, self.db_path]
        for widget in widgets:
            signal = (widget.currentIndexChanged if isinstance(widget, QtWidgets.QComboBox) else
                      widget.valueChanged if isinstance(widget, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)) else
                      widget.textChanged)
            signal.connect(lambda *args: self.settings_timer.start())
        self.controls.currentChanged.connect(lambda *args: self.settings_timer.start())
        self.result_tabs.currentChanged.connect(lambda *args: self.settings_timer.start())

    def store(self):
        if self.mode.currentIndex() == 0:
            return Store(ROOT / "data" / "demo.db")
        if not self.db_path.text().strip():
            raise ValueError("Укажите путь к базе")
        return Store(self.db_path.text())

    def choose_db(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Существующая база SQLite", self.db_path.text(), "SQLite (*.db *.sqlite *.sqlite3);;Все файлы (*)")
        if path:
            self.db_path.setText(path)
            self.save_settings()

    def error(self, message):
        self.log.appendPlainText("ОШИБКА: " + message)
        self.status.setText(message.splitlines()[0])
        QtWidgets.QMessageBox.warning(self, "GSM7-20H10", message)

    def launch(self, recipe=None, metadata=None):
        if self.worker is not None:
            return
        if not self.persist_settings():
            return
        self.controls.setEnabled(False)
        self.stop_button.setEnabled(recipe is not None)
        self.worker = Worker(self.settings(), recipe, metadata)
        self.worker.log.connect(self.log.appendPlainText)
        self.worker.point.connect(self.add_point)
        self.worker.result.connect(self.save_result)
        self.worker.identified.connect(lambda value: self.status.setText(value))
        self.worker.failed.connect(self.error)
        self.worker.finished.connect(self.finished)
        self.worker.start()

    def identify(self):
        self.launch()

    def start_measurement(self):
        try:
            diode = self.kind.currentIndex() == 0
            levels = tuple(float(v.strip().replace(",", ".")) / (1000 if diode else 1)
                           for v in self.levels.text().split(";"))
            recipe = Recipe("diode" if diode else "j310", levels,
                            self.limit.value() / (1 if diode else 1000), self.delay.value(), self.temperature.value(),
                            self.wire_mode.currentData(), self.terminals.currentData())
            recipe.validate()
            metadata = (self.sample.text().strip(), self.model.text().strip(), self.batch.text().strip())
            if not all(metadata) or metadata[1] == "Не указана":
                raise ValueError("Заполните номер, маркировку компонента и партию")
            self.active_store = self.store()
            # Check storage before enabling source output.
            self.active_store.initialize()
            self.points = []
            self.plot.clear()
            self.curve = self.plot.plot(pen=pg.mkPen("#45d7d0", width=2), symbol="o", name=metadata[0])
            self.progress.setRange(0, len(levels))
            self.progress.setValue(0)
            self.launch(recipe, metadata)
        except Exception as error:
            self.error(str(error))

    def add_point(self, point):
        self.points.append(point)
        voltage = [p.voltage for p in self.points]
        current = [p.current for p in self.points]
        x, y = (current, voltage) if self.axes_swapped else (voltage, current)
        self.curve.setData(x, y)
        self.progress.setValue(len(self.points))

    def swap_axes(self):
        self.axes_swapped = not self.axes_swapped
        for curve in self.plot.listDataItems():
            x, y = curve.getData()
            if x is not None and y is not None:
                curve.setData(y, x)
        if self.axes_swapped:
            self.plot.setLabel("bottom", "Ток", units="A")
            self.plot.setLabel("left", "Напряжение", units="V")
        else:
            self.plot.setLabel("bottom", "Напряжение", units="V")
            self.plot.setLabel("left", "Ток", units="A")
        self.swap_axes_button.setText("Поменять оси · " + ("X: I / Y: U" if self.axes_swapped else "X: U / Y: I"))
        self.plot.autoRange()
        if hasattr(self, "settings_timer"):
            self.settings_timer.start()

    def save_result(self, run):
        recovery = ROOT / "data" / "pending" / (run["id"] + ".json")
        try:
            recovery.parent.mkdir(parents=True, exist_ok=True)
            recovery.write_text(json.dumps(dict(database=str(self.active_store.path), run=run),
                                           ensure_ascii=False, indent=4), encoding="utf-8")
            self.active_store.save_many([run])
            recovery.unlink()
            self.status.setText(f"{run['sample']}: {run['state']} · сохранено в {self.active_store.path}")
            self.refresh()
        except Exception as error:
            self.error(f"Не удалось сохранить в БД: {error}\nРезерв результата: {recovery}")

    def stop_measurement(self):
        if self.worker:
            self.worker.stop.set()
            self.status.setText("Останавливаю серию; ожидаю подтверждение OUTPUT OFF…")

    def finished(self):
        self.worker.deleteLater()
        self.worker = None
        self.controls.setEnabled(True)
        self.stop_button.setEnabled(False)

    def refresh(self):
        if not hasattr(self, "table"):
            return
        try:
            blocker = QtCore.QSignalBlocker(self.table)
            self.runs = self.store().load()
            self.table.setRowCount(len(self.runs))
            for row, run in enumerate(self.runs):
                last = run["points"][-1] if run["points"] else None
                result = "—"
                if last:
                    result = (f"Vf = {last['voltage']:.6f} В" if run["recipe"]["kind"] == "diode" else
                              f"Id = {last['current'] * 1000:.3f} мА @ {last['voltage']:.3f} В")
                values = [run["sample"], run["model"], run["batch"], run["recipe"]["kind"],
                          run["measured_at"][:19], run["state"], str(len(run["points"])), result]
                for column, value in enumerate(values):
                    self.table.setItem(row, column, QtWidgets.QTableWidgetItem(value))
            self.badge.setText("●  ДЕМО" if self.mode.currentIndex() == 0 else "●  ПРИБОР · " + self.host.text())
            del blocker
        except Exception as error:
            self.error(str(error))

    def selected(self):
        return [self.runs[index.row()] for index in self.table.selectionModel().selectedRows()]

    def plot_selected(self):
        if self.worker is not None:
            return
        self.plot.clear()
        colors = ["#45d7d0", "#f4bd61", "#8fb6ff", "#e591c8", "#a4d66f"]
        for index, run in enumerate(self.selected()):
            voltage = [p["voltage"] for p in run["points"]]
            current = [p["current"] for p in run["points"]]
            x, y = (current, voltage) if self.axes_swapped else (voltage, current)
            self.plot.plot(x, y,
                           pen=pg.mkPen(colors[index % len(colors)], width=2), symbol="o", name=run["sample"])

    def match(self):
        try:
            runs = self.selected()
            size = int(self.group_size.currentText())
            if len(runs) < size:
                raise ValueError(f"Выберите минимум {size} строки во вкладке «Измерения» (Ctrl/Shift)")
            groups = match_groups(runs, size, self.tolerance.value() / 1000)
            lines = ["Максимальный разброс по всем точкам. Группы-кандидаты могут пересекаться.",
                     "Сравниваются только одинаковые марки, партии, условия и температуры.", ""]
            for spread, group in groups:
                unit = "мВ" if group[0]["recipe"]["kind"] == "diode" else "мА"
                lines.append(f"{' + '.join(r['sample'] for r in group)}: Δmax = {spread * 1000:.4f} {unit}")
            self.matches.setPlainText("\n".join(lines) + ("\nПодходящих групп нет." if not groups else ""))
        except Exception as error:
            self.error(str(error))

    def export_csv(self):
        runs = self.selected()
        if not runs:
            self.error("Выберите измерения для экспорта")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "CSV измерений", "measurements.csv", "CSV (*.csv)")
        if path:
            try:
                with open(path, "w", encoding="utf-8-sig", newline="") as stream:
                    writer = csv.writer(stream, delimiter=";")
                    writer.writerow(["run_id", "sample", "model", "batch", "kind", "demo", "state", "setpoint_SI", "voltage_V", "current_A", "status"])
                    for run in runs:
                        for point in run["points"]:
                            writer.writerow([run["id"], run["sample"], run["model"], run["batch"], run["recipe"]["kind"],
                                             run["demo"], run["state"], point["setpoint"], point["voltage"], point["current"], point["status"]])
            except Exception as error:
                self.error(str(error))

    def export_runs(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Обмен между ПК", "gsm-measurements.json", "JSON (*.json)")
        if path:
            try:
                self.store().export_json(path)
                self.status.setText("Экспортировано: " + path)
            except Exception as error:
                self.error(str(error))

    def import_runs(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Импорт измерений", "", "JSON (*.json)")
        if path:
            try:
                payload = json.loads(Path(path).read_text(encoding="utf-8"))
                if any(r["demo"] != (self.mode.currentIndex() == 0) for r in payload["runs"]):
                    raise ValueError("Демо и реальные измерения импортируются в соответствующем режиме отдельно")
                count = self.store().import_json(path)
                self.refresh()
                self.status.setText(f"Добавлено измерений: {count}")
            except Exception as error:
                self.error(str(error))

    def recover(self):
        try:
            count = 0
            for path in (ROOT / "data" / "pending").glob("*.json"):
                pending = json.loads(path.read_text(encoding="utf-8"))
                if Path(pending["database"]).resolve() != self.store().path:
                    continue
                self.store().save_many([pending["run"]])
                path.unlink()
                count += 1
            self.refresh()
            self.status.setText(f"Восстановлено в выбранную БД: {count}")
        except Exception as error:
            self.error(str(error))

    def closeEvent(self, event):
        if self.worker is not None:
            self.stop_measurement()
            event.ignore()
            self.status.setText("Дождитесь завершения отключения выхода, затем закройте окно")
        else:
            self.settings_timer.stop()
            if not self.persist_settings():
                event.ignore()
                return
            event.accept()
