"""Qt desktop workbench with background I/O and a durable recovery queue."""

import csv
from html import escape
from dataclasses import asdict
import json
from pathlib import Path
import threading

from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

from .core import (Point, Recipe, format_level, generate_levels, match_groups, match_pair_sums,
                   quantize_levels, simulate)
from . import __version__
from .instrument import Instrument, SocketTransport, VisaTransport
from .storage import Store, new_run
from .help_ui import AboutDialog, HelpDialog
from .rsa_ui import RsaWindow


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
QProgressBar
{
    background: #192536;
    color: #ffffff;
    border: 1px solid #405570;
    border-radius: 4px;
    text-align: center;
    min-height: 22px;
}
QProgressBar::chunk
{
    background: #17645f;
    border-radius: 3px;
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


class ColumnFilterDialog(QtWidgets.QDialog):
    """Choose multiple column values without changing the filter on Cancel."""

    def __init__(self, title, values, selected, parent):
        super().__init__(parent)
        self.setWindowTitle("Фильтр · " + title)
        self.resize(360, 440)
        layout = QtWidgets.QVBoxLayout(self)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Найти значение…")
        layout.addWidget(self.search)
        self.values = QtWidgets.QListWidget()
        for value in values:
            item = QtWidgets.QListWidgetItem(value or "(Пусто)", self.values)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, value)
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.CheckState.Checked if selected is None or value in selected
                               else QtCore.Qt.CheckState.Unchecked)
        layout.addWidget(self.values)
        row = QtWidgets.QHBoxLayout()
        layout.addLayout(row)
        for label, state in (("Выбрать найденные", True), ("Снять найденные", False)):
            button = QtWidgets.QPushButton(label)
            button.clicked.connect(lambda checked=False, state=state: self.check_visible(state))
            row.addWidget(button)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Apply |
                                            QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Apply).setText("Применить")
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Apply).clicked.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.search.textChanged.connect(self.search_values)

    def search_values(self, text):
        for index in range(self.values.count()):
            item = self.values.item(index)
            item.setHidden(text.casefold() not in item.text().casefold())

    def check_visible(self, checked):
        for index in range(self.values.count()):
            item = self.values.item(index)
            if not item.isHidden():
                item.setCheckState(QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked)

    def selected_values(self):
        return {self.values.item(index).data(QtCore.Qt.ItemDataRole.UserRole)
                for index in range(self.values.count())
                if self.values.item(index).checkState() == QtCore.Qt.CheckState.Checked}


class Window(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.worker = None
        self.runs = []
        self.column_filters = {}
        self.points = []
        self.axes_swapped = False
        self.hover_selection = None
        self.active_store = None
        self.initial_position_set = False
        self.help_dialog = None
        self.about_dialog = None
        self.rsa_window = None
        self.rsa_settings = {}
        self.setWindowIcon(QtGui.QIcon(str(ICON_PATH)))
        self.setWindowTitle(f"GSM7-20H10 · Компоненты и смесители · v{__version__}")
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
        instruments_menu = self.menuBar().addMenu("Приборы")
        self.rsa_action = instruments_menu.addAction("Смесители · RSA6265 + SMU200A", self.show_rsa)
        instruments_menu.addAction("R&S SMU200A · Состояние выходов…", self.show_smu)
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

    def show_rsa(self):
        if self.rsa_window is None:
            self.rsa_window = RsaWindow(self, ROOT, self.rsa_settings)
            self.rsa_window.settings_changed.connect(self.save_rsa_settings)
            self.mixers_layout.addWidget(self.rsa_window)
        self.workspaces.setCurrentWidget(self.mixers_page)

    def workspace_changed(self, index):
        if index == 1:
            self.show_rsa()

    def save_rsa_settings(self, settings):
        self.rsa_settings = settings
        self.settings_timer.start()

    def show_smu(self):
        self.show_rsa()
        self.rsa_window.setup_tabs.setCurrentIndex(1)
        self.rsa_window.launch("smu_identify")

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
        self.workspaces = QtWidgets.QTabWidget()
        self.setCentralWidget(self.workspaces)
        root = QtWidgets.QWidget()
        self.workspaces.addTab(root, "Диоды и транзисторы")
        self.mixers_page = QtWidgets.QWidget()
        self.mixers_layout = QtWidgets.QVBoxLayout(self.mixers_page)
        self.workspaces.addTab(self.mixers_page, "Смесители")
        self.workspaces.currentChanged.connect(self.workspace_changed)
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
        self.nplc = QtWidgets.QDoubleSpinBox()
        self.nplc.setDecimals(2)
        self.nplc.setRange(0.01, 10)
        self.nplc.setValue(1)
        self.nplc.setToolTip("Число периодов сети для интегрирования. При 50 Гц: 1 NPLC = 20 мс.")
        self.digits = QtWidgets.QComboBox()
        for digits in (3.5, 4.5, 5.5, 6.5):
            self.digits.addItem(f"{int(digits)}½", digits)
        self.digits.setCurrentIndex(2)
        self.digits.setToolTip("Разряды дисплея прибора. Скорость и подавление шума задаются NPLC.")
        form.addRow("NPLC", self.nplc)
        form.addRow("Разрядность", self.digits)
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
        self.button("Создать новую базу…", self.create_db, db_layout)
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
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFormat("%v / %m точек")
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
        self.reset_scale_button = self.button("Сбросить масштаб", self.reset_plot_scale, plot_toolbar)
        self.reset_scale_button.setToolTip("Показать все отображаемые точки · Ctrl+0")
        self.reset_scale_action = QtGui.QAction(self)
        self.reset_scale_action.setShortcut("Ctrl+0")
        self.reset_scale_action.triggered.connect(self.reset_plot_scale)
        self.addAction(self.reset_scale_action)
        self.swap_axes_button = self.button("Поменять оси · X: U / Y: I", self.swap_axes, plot_toolbar)
        pg.setConfigOptions(antialias=True, background="#111923", foreground="#aabbd0")
        self.plot = pg.PlotWidget(title="Вольт-амперные характеристики")
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.setLabel("bottom", "Напряжение", units="V")
        self.plot.setLabel("left", "Ток", units="A")
        self.plot.addLegend()
        self.hover_lines = [QtWidgets.QGraphicsLineItem(), QtWidgets.QGraphicsLineItem()]
        for line in self.hover_lines:
            line.setPen(pg.mkPen("#f4bd61", width=1, style=QtCore.Qt.PenStyle.DashLine))
            line.setZValue(20)
            self.plot.addItem(line, ignoreBounds=True)
            line.hide()
        self.hover_label = QtWidgets.QLabel(self.plot.viewport())
        self.hover_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.hover_label.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hover_label.setStyleSheet("""
            QLabel
            {
                background: #223247;
                color: #ffffff;
                border: 1px solid #f4bd61;
                border-radius: 5px;
                padding: 7px;
            }
        """)
        self.hover_label.hide()
        self.plot.viewport().installEventFilter(self)
        self.plot.getViewBox().sigRangeChanged.connect(self.update_hover_lines)
        plot_layout.addWidget(self.plot)
        splitter.addWidget(plot_panel)
        tabs = QtWidgets.QTabWidget()
        self.result_tabs = tabs
        splitter.addWidget(tabs)
        self.table = QtWidgets.QTableWidget(0, 8)
        self.table_headers = ["Образец", "Марка", "Партия", "Метод", "Дата UTC", "Статус", "Точек", "Результат"]
        self.table.setHorizontalHeaderLabels(self.table_headers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionsClickable(True)
        self.table.horizontalHeader().sectionClicked.connect(self.show_column_filter)
        self.table.itemSelectionChanged.connect(self.plot_selected)
        table_page = QtWidgets.QWidget()
        table_layout = QtWidgets.QVBoxLayout(table_page)
        filter_row = QtWidgets.QHBoxLayout()
        self.table_search = QtWidgets.QLineEdit()
        self.table_search.setPlaceholderText("Поиск по всем столбцам…")
        self.table_search.setClearButtonEnabled(True)
        self.table_search.textChanged.connect(self.apply_filters)
        filter_row.addWidget(self.table_search, 1)
        self.filter_count = QtWidgets.QLabel()
        filter_row.addWidget(self.filter_count)
        self.button("Сбросить фильтры", self.clear_filters, filter_row)
        table_layout.addLayout(filter_row)
        table_layout.addWidget(QtWidgets.QLabel("Нажмите заголовок столбца ▼ для выбора значений фильтра."))
        table_layout.addWidget(self.table)
        row = QtWidgets.QHBoxLayout()
        table_layout.addLayout(row)
        self.button("Обновить", self.refresh, row)
        self.button("CSV выбранных измерений…", self.export_csv, row)
        tabs.addTab(table_page, "Измерения")
        matching_page = QtWidgets.QWidget()
        matching_layout = QtWidgets.QVBoxLayout(matching_page)
        method_row = QtWidgets.QHBoxLayout()
        matching_layout.addLayout(method_row)
        method_row.addWidget(QtWidgets.QLabel("Критерий"))
        self.match_method = QtWidgets.QComboBox()
        self.match_method.addItem("Близость отдельных компонентов", "individual")
        self.match_method.addItem("Диоды · баланс сумм двух пар", "pair_sums")
        method_row.addWidget(self.match_method, 1)
        self.match_method.currentIndexChanged.connect(self.update_match_method)
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
        self.tolerance_label = QtWidgets.QLabel("Допуск: мВ (диоды) / мА (J310)")
        row.addWidget(self.tolerance_label)
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

    def update_match_method(self):
        balanced = self.match_method.currentData() == "pair_sums"
        self.group_size.setEnabled(not balanced)
        self.tolerance_label.setText("Допуск сумм, мВ" if balanced else "Допуск: мВ (диоды) / мА (J310)")
        if balanced:
            self.group_size.setCurrentText("4")

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
            kind = "diode" if self.kind.currentIndex() == 0 else "j310"
            points = quantize_levels(tuple(value / (1000 if kind == "diode" else 1) for value in points), kind)
            self.levels.setText("; ".join(format_level(value, kind) for value in points))
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
                    result_tab=self.result_tabs.currentIndex(), workspace_tab=self.workspaces.currentIndex(),
                    table_search=self.table_search.text(),
                    column_filters={str(column): sorted(values) for column, values in self.column_filters.items()},
                    rsa=self.rsa_window.settings() if self.rsa_window is not None else self.rsa_settings)

    def setting_widgets(self):
        return dict(kind=self.kind, sample=self.sample, model=self.model, batch=self.batch,
                    spacing=self.spacing, range_start=self.range_start, range_stop=self.range_stop,
                    point_count=self.point_count, levels=self.levels, limit=self.limit,
                    delay=self.delay, temperature=self.temperature, wires=self.wire_mode,
                    terminals=self.terminals, nplc=self.nplc, digits=self.digits,
                    group_size=self.group_size, tolerance=self.tolerance, match_method=self.match_method)

    def load_settings(self):
        if SETTINGS.exists():
            try:
                data = json.loads(SETTINGS.read_text(encoding="utf-8"))
                if isinstance(data.get("rsa"), dict):
                    self.rsa_settings = data["rsa"]
                self.column_filters = {int(column): set(values)
                                       for column, values in data.get("column_filters", {}).items()
                                       if column.isdigit() and 0 <= int(column) < self.table.columnCount()
                                       and isinstance(values, list) and all(isinstance(value, str) for value in values)}
                self.table_search.setText(str(data.get("table_search", "")))
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
                # Older settings contain unrounded generated values. Normalize
                # only the editable field; historical measurements stay intact.
                try:
                    self.normalize_levels_field()
                except ValueError:
                    pass
                self.update_levels_tooltip()
                if data.get("workspace_tab") == 1:
                    self.workspaces.setCurrentIndex(1)
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
        self.workspaces.currentChanged.connect(lambda *args: self.settings_timer.start())
        self.table_search.textChanged.connect(lambda *args: self.settings_timer.start())

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

    def create_db(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Новая база SQLite", self.db_path.text(), "SQLite (*.db)")
        if path:
            try:
                store = Store(path)
                store.initialize()
                self.db_path.setText(str(store.path))
                self.save_settings()
                self.status.setText("База готова: " + str(store.path))
            except Exception as error:
                self.error(str(error))

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
            kind = "diode" if diode else "j310"
            levels = self.normalize_levels_field()
            recipe = Recipe(kind, levels,
                            self.limit.value() / (1 if diode else 1000), self.delay.value(), self.temperature.value(),
                            self.wire_mode.currentData(), self.terminals.currentData(),
                            self.nplc.value(), self.digits.currentData())
            recipe.validate()
            self.levels.setText("; ".join(format_level(value, kind) for value in levels))
            metadata = (self.sample.text().strip(), self.model.text().strip(), self.batch.text().strip())
            if not all(metadata) or metadata[1] == "Не указана":
                raise ValueError("Заполните номер, маркировку компонента и партию")
            self.active_store = self.store()
            # Check storage before enabling source output.
            self.active_store.initialize()
            self.points = []
            self.clear_plot()
            self.curve = self.create_curve(metadata[0], "#45d7d0")
            self.progress.setRange(0, len(levels))
            self.progress.setValue(0)
            self.launch(recipe, metadata)
        except Exception as error:
            self.error(str(error))

    def normalize_levels_field(self):
        kind = "diode" if self.kind.currentIndex() == 0 else "j310"
        levels = tuple(float(value.strip().replace(",", ".")) / (1000 if kind == "diode" else 1)
                       for value in self.levels.text().split(";"))
        levels = quantize_levels(levels, kind)
        self.levels.setText("; ".join(format_level(value, kind) for value in levels))
        self.levels.setCursorPosition(0)
        return levels

    def add_point(self, point):
        self.points.append(point)
        voltage = [p.voltage for p in self.points]
        current = [p.current for p in self.points]
        x, y = (current, voltage) if self.axes_swapped else (voltage, current)
        self.curve.setData(x, y, data=list(self.points))
        self.progress.setValue(len(self.points))

    def swap_axes(self):
        self.hide_hover()
        self.axes_swapped = not self.axes_swapped
        for curve in self.plot.listDataItems():
            x, y = curve.getData()
            if x is not None and y is not None:
                metadata = [point.data() for point in curve.scatter.points()]
                curve.setData(y, x, data=metadata)
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

    def reset_plot_scale(self):
        self.hide_hover()
        self.plot.autoRange()
        self.plot.enableAutoRange(x=True, y=True)

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
            selected_ids = {run["id"] for run in self.selected()}
            blocker = QtCore.QSignalBlocker(self.table)
            self.runs = self.store().load()
            self.table.clearSelection()
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
                if run["id"] in selected_ids:
                    self.table.selectionModel().select(self.table.model().index(row, 0),
                        QtCore.QItemSelectionModel.SelectionFlag.Select | QtCore.QItemSelectionModel.SelectionFlag.Rows)
            self.badge.setText("●  ДЕМО" if self.mode.currentIndex() == 0 else "●  ПРИБОР · " + self.host.text())
            self.apply_filters(update_plot=False)
            del blocker
        except Exception as error:
            self.error(str(error))

    def selected(self):
        return [self.runs[index.row()] for index in self.table.selectionModel().selectedRows()
                if not self.table.isRowHidden(index.row())]

    def show_column_filter(self, column):
        values = sorted({self.table.item(row, column).text() for row in range(self.table.rowCount())},
                        key=str.casefold)
        dialog = ColumnFilterDialog(self.table_headers[column], values, self.column_filters.get(column), self)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            selected = dialog.selected_values()
            if selected == set(values):
                self.column_filters.pop(column, None)
            else:
                self.column_filters[column] = selected
            self.apply_filters()
            self.settings_timer.start()

    def clear_filters(self):
        self.column_filters.clear()
        self.table_search.clear()
        self.apply_filters()
        self.settings_timer.start()

    def apply_filters(self, *args, update_plot=True):
        search = self.table_search.text().strip().casefold()
        visible = 0
        selection_changed = False
        blocker = QtCore.QSignalBlocker(self.table)
        for row in range(self.table.rowCount()):
            values = [self.table.item(row, column).text() for column in range(self.table.columnCount())]
            matches = (not search or any(search in value.casefold() for value in values)) and all(
                values[column] in allowed for column, allowed in self.column_filters.items())
            self.table.setRowHidden(row, not matches)
            visible += int(matches)
            if not matches and any(self.table.item(row, column).isSelected()
                                   for column in range(self.table.columnCount())):
                self.table.selectionModel().select(self.table.model().index(row, 0),
                    QtCore.QItemSelectionModel.SelectionFlag.Deselect | QtCore.QItemSelectionModel.SelectionFlag.Rows)
                selection_changed = True
        for column, title in enumerate(self.table_headers):
            header = self.table.horizontalHeaderItem(column)
            header.setText(title + (" ● ▼" if column in self.column_filters else " ▼"))
            header.setToolTip("Выбрать значения · " + title)
        self.filter_count.setText(f"Показано: {visible} / {self.table.rowCount()}")
        del blocker
        if update_plot and selection_changed:
            self.plot_selected()

    def plot_selected(self):
        if self.worker is not None:
            return
        self.clear_plot()
        colors = ["#45d7d0", "#f4bd61", "#8fb6ff", "#e591c8", "#a4d66f"]
        for index, run in enumerate(self.selected()):
            voltage = [p["voltage"] for p in run["points"]]
            current = [p["current"] for p in run["points"]]
            x, y = (current, voltage) if self.axes_swapped else (voltage, current)
            curve = self.create_curve(run["sample"], colors[index % len(colors)])
            curve.setData(x, y, data=[Point(**point) for point in run["points"]])

    def clear_plot(self):
        self.hide_hover()
        self.plot.clear()
        for line in self.hover_lines:
            self.plot.addItem(line, ignoreBounds=True)

    def create_curve(self, name, color):
        curve = self.plot.plot(pen=pg.mkPen(color, width=2), symbol="o", name=name,
                               symbolSize=8)
        curve.scatter.setData(hoverable=True, tip=None, hoverSize=12, hoverBrush=pg.mkBrush("#f4bd61"))
        curve.scatter.sigHovered.connect(lambda item, points, event: self.hover_points(curve, points))
        return curve

    def hover_points(self, curve, points):
        if len(points) == 0:
            if self.hover_selection is not None and self.hover_selection[0] is curve:
                self.hide_hover()
            return
        point = points[0].data()
        if not isinstance(point, Point):
            return
        self.hover_selection = (curve, point)
        voltage = pg.siFormat(point.voltage, precision=6, suffix="V")
        current = pg.siFormat(point.current, precision=6, suffix="A")
        # A regular overlay has no QToolTip expiry timer and does not take hover.
        self.hover_label.setText(f"{curve.name()}\nU = {voltage}\nI = {current}")
        self.hover_label.adjustSize()
        self.hover_label.show()
        self.hover_label.raise_()
        self.update_hover_lines()

    def eventFilter(self, watched, event):
        if hasattr(self, "plot") and watched is self.plot.viewport():
            if event.type() in (QtCore.QEvent.Type.Leave, QtCore.QEvent.Type.Hide):
                self.hide_hover()
        return super().eventFilter(watched, event)

    def update_hover_lines(self, *args):
        if self.hover_selection is None:
            return
        point = self.hover_selection[1]
        x, y = (point.current, point.voltage) if self.axes_swapped else (point.voltage, point.current)
        (left, right), (bottom, top) = self.plot.getViewBox().viewRange()
        self.hover_lines[0].setLine(x, bottom, x, y)
        self.hover_lines[1].setLine(left, y, x, y)
        for line in self.hover_lines:
            line.show()
        position = self.plot.mapFromScene(self.plot.getViewBox().mapViewToScene(QtCore.QPointF(x, y)))
        viewport = self.plot.viewport()
        # Choose a side before clamping: clamping a right-hand label at the
        # viewport edge can otherwise slide it directly over the hovered point.
        width, height = self.hover_label.width(), self.hover_label.height()
        gap = 18
        left = position.x() + gap
        if left + width > viewport.width() - 4:
            left = position.x() - width - gap
        top = position.y() - height - gap
        if top < 4:
            top = position.y() + gap
        left = max(4, min(left, viewport.width() - width - 4))
        top = max(4, min(top, viewport.height() - height - 4))
        self.hover_label.move(left, top)

    def hide_hover(self):
        self.hover_selection = None
        if hasattr(self, "hover_lines"):
            for line in self.hover_lines:
                line.hide()
        if hasattr(self, "hover_label"):
            self.hover_label.hide()

    def match(self):
        try:
            runs = self.selected()
            if self.match_method.currentData() == "pair_sums":
                if len(runs) < 4:
                    raise ValueError("Для баланса двух пар выберите минимум четыре разных диода")
                if any(run["recipe"]["kind"] != "diode" for run in runs):
                    raise ValueError("Баланс сумм предназначен только для диодов")
                groups = match_pair_sums(runs, self.tolerance.value() / 1000)
                lines = ["Баланс двух пар: |(Vf1 + Vf2) − (Vf3 + Vf4)| на каждой точке тока.",
                         "Схема в F1 → Смеситель: верхняя пара D1 + D2, нижняя D3 + D4.",
                         "Критерий сумм не проверяет близость отдельных диодов. Варианты могут пересекаться.", ""]
                for spread, (first, second) in groups:
                    left = " + ".join(run["sample"] for run in first)
                    right = " + ".join(run["sample"] for run in second)
                    lines.append(f"({left}) ↔ ({right}): ΔΣmax = {spread * 1000:.4f} мВ")
                    positions = zip(("D1", "D2", "D3", "D4"), (*first, *second))
                    lines.append("  Установка: " + "; ".join(
                        f"{position} схемы ← образец «{run['sample']}»" for position, run in positions))
                    lines.append("")
                self.matches.setPlainText("\n".join(lines) + ("\nПодходящих пар нет." if not groups else ""))
                return
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
        if self.rsa_window is not None and (self.rsa_window.worker is not None or self.rsa_window.generator_active or
                self.rsa_window.two_tone.worker is not None or self.rsa_window.two_tone.unsafe_outputs):
            self.rsa_window.stop()
            if self.worker is not None:
                self.stop_measurement()
            event.ignore()
            self.status.setText("Дождитесь завершения стенда и подтверждения RF OFF на SMU, затем закройте окно")
            return
        if self.worker is not None:
            self.stop_measurement()
            event.ignore()
            self.status.setText("Дождитесь завершения отключения выхода, затем закройте окно")
        else:
            if self.rsa_window is not None:
                self.rsa_window.close()
            self.settings_timer.stop()
            if not self.persist_settings():
                event.ignore()
                return
            event.accept()
