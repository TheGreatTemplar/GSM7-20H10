"""E7-29 measurement workspace; all serial traffic runs outside the UI thread."""

import csv
from datetime import datetime, timezone
import json
import math
import threading

from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg
from serial.tools import list_ports

from .e729 import E729, PARAMETERS, impedance_parameters
from .form_layout import compact_inputs
from .plot_hover import PointHover
from .header_hints import HeaderHints
from .instrument_buttons import connection_test_button
from .rlc_storage import RlcStore
from .rlc_uncertainty import FIXTURES, measurement_uncertainty
from .table_filters import FilterHeader, configure_table


STATES = dict(running='Незавершено', complete='Завершено', cancelled='Остановлено', failed='Ошибка')


class RlcWorker(QtCore.QThread):
    log = QtCore.Signal(str)
    status = QtCore.Signal(str)
    point = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, owner, settings, operation, path):
        super().__init__(owner)
        self.config, self.operation, self.path = settings, operation, path
        self.stop = threading.Event()

    def run(self):
        meter, store, identifier = None, None, None
        try:
            config = dict(self.config)
            if not config['demo']:
                meter = E729(config['port'], self.log.emit)
            config['identity'] = meter.identity if meter else 'ДЕМО · Е7-29'
            self.status.emit(config['identity'])
            if self.operation == 'identify':
                return
            store = RlcStore(self.path)
            identifier = store.start(config)
            frequencies = [None] if self.operation == 'read' else config['frequencies']
            if meter and self.operation != 'read':
                meter.set_range(config['range_index'])
                meter.set_speed(config['speed'])
            for index, frequency in enumerate(frequencies):
                if self.stop.is_set():
                    raise InterruptedError()
                self.status.emit(f'Е7-29 · точка {index + 1}/{len(frequencies)}')
                if meter:
                    if frequency is not None:
                        meter.set_frequency(frequency)
                        if self.stop.wait(config['settling_s']):
                            raise InterruptedError()
                        # Discard a completed conversion that may predate the new setting.
                        meter.read_measurement(self.stop, frequency)
                    point = meter.read_measurement(self.stop, frequency)
                else:
                    if self.stop.wait(0.05):
                        raise InterruptedError()
                    actual_frequency = frequency or 1000000
                    reactance = 2 * math.pi * actual_frequency * 10e-6
                    magnitude = math.hypot(2, reactance)
                    range_index = next((index for index, upper in ((6, 1), (5, 10), (4, 100),
                        (3, 1000), (2, 10000), (1, 100000)) if magnitude <= upper), 0)
                    point = dict(measured_at=datetime.now(timezone.utc).isoformat(),
                        frequency_hz=actual_frequency, bias_v=0, flags=0x81,
                        mode=0, speed=config['speed'], range_index=range_index, autorange=True,
                        auto_parameter=False, equivalent_circuit='series', overloaded=False,
                        status='measured', raw_hex=None,
                        values=impedance_parameters(actual_frequency, math.hypot(2, reactance),
                                                    math.atan2(reactance, 2)))
                point['position'] = index + 1
                point['series_id'] = identifier
                point['demo'] = config['demo']
                point['uncertainty'] = measurement_uncertainty(point, config.get('fixture_id'), config.get('operator_ac_level_v'))
                store.append(identifier, index, point)
                self.point.emit(point)
            store.finish(identifier, 'complete')
            self.status.emit(f'Сохранено точек: {len(frequencies)} · {self.path}')
        except InterruptedError:
            if identifier:
                store.finish(identifier, 'cancelled')
            self.status.emit('Остановлено. Полученные точки сохранены.')
        except Exception as error:
            if identifier:
                try:
                    store.finish(identifier, 'failed', str(error))
                except Exception as storage_error:
                    self.log.emit('Не удалось записать статус: ' + str(storage_error))
            self.failed.emit(str(error))
        finally:
            if meter:
                meter.close()


class RlcPanel(QtWidgets.QWidget):
    settings_changed = QtCore.Signal(dict)

    def __init__(self, parent, root, settings=None):
        super().__init__(parent)
        self.root, self.worker = root, None
        self.points, self.archive_rows, self.filters = [], [], {}
        self.build()
        self.restore(settings or {})
        self.refresh_archive()
        for widget in self.fields.values():
            signal = (widget.toggled if isinstance(widget, QtWidgets.QCheckBox) else
                      widget.currentIndexChanged if isinstance(widget, QtWidgets.QComboBox) else
                      widget.valueChanged if isinstance(widget, QtWidgets.QAbstractSpinBox) else widget.textChanged)
            signal.connect(self.changed)
        self.splitter.splitterMoved.connect(self.changed)
        self.result_tabs.currentChanged.connect(self.changed)
        self.tabs.currentChanged.connect(self.changed)

    def build(self):
        outer = QtWidgets.QVBoxLayout(self)
        heading = QtWidgets.QLabel('LCR · Е7-29')
        heading.setStyleSheet('font-size:20px; font-weight:700')
        outer.addWidget(heading)
        self.tabs = QtWidgets.QTabWidget()
        outer.addWidget(self.tabs)
        measurement = QtWidgets.QWidget()
        self.tabs.addTab(measurement, 'Измерение')
        main = QtWidgets.QHBoxLayout(measurement)
        controls = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(controls)
        form.setContentsMargins(8, 8, 8, 8)
        self.port = QtWidgets.QComboBox()
        self.port.setEditable(True)
        self.port.addItems(sorted(set(['COM9'] + [item.device for item in list_ports.comports()])))
        self.port.setCurrentIndex(self.port.findText('COM9'))
        self.port.setToolTip('USB-мост CP210x. Е7-29: 9600 бод, 8N1, без управления потоком.\nПорт можно ввести вручную; номер виден в Диспетчере устройств Windows.')
        connection_row = QtWidgets.QHBoxLayout()
        connection_row.addWidget(self.port)
        self.connection_button = connection_test_button('Проверить связь Е7-29',
            lambda: self.launch('identify'), 'Читает идентификатор прибора без изменения настроек.')
        connection_row.addWidget(self.connection_button)
        connection_row.addStretch()
        form.addRow('COM-порт', connection_row)
        self.demo = QtWidgets.QCheckBox('Демо · отдельная база')
        self.connection_button.bind_endpoint(self.port, self.demo)
        form.addRow(self.demo)
        self.sample = QtWidgets.QLineEdit()
        self.model = QtWidgets.QLineEdit()
        self.batch = QtWidgets.QLineEdit()
        self.notes = QtWidgets.QLineEdit()
        for title, widget in [('Образец', self.sample), ('Маркировка', self.model), ('Партия', self.batch), ('Примечание', self.notes)]:
            form.addRow(title, widget)
        self.frequency = self.number(50, 15000, 1000, 3)
        self.end_frequency = self.number(50, 15000, 15000, 3)
        self.count = QtWidgets.QSpinBox()
        self.count.setRange(2, 1000)
        self.count.setValue(20)
        self.spacing = QtWidgets.QComboBox()
        self.spacing.addItems(['Логарифмический', 'Линейный'])
        self.measurement_range = QtWidgets.QComboBox()
        self.measurement_range.addItems(['Авто', '1 МОм', '100 кОм', '10 кОм', '1 кОм', '100 Ом', '10 Ом', '1 Ом'])
        self.speed = QtWidgets.QComboBox()
        self.speed.addItems(['Быстро', 'Норма'])
        self.speed.setCurrentIndex(1)
        self.settling = self.number(0.1, 60, 0.5, 1)
        self.signal_level = QtWidgets.QLineEdit()
        self.signal_level.setPlaceholderText('Не записан')
        self.signal_level.setToolTip('Необязательная запись уровня с панели прибора, в вольтах.\nЭто условие опыта: программа не считывает и не изменяет уровень AC.\nСмещение DC считывается из ответа прибора.')
        self.fixture = QtWidgets.QComboBox()
        for name, identifier in FIXTURES:
            self.fixture.addItem(name, identifier)
        self.fixture.setToolTip('Подключённое контактное / присоединительное устройство.\nКоэффициент A4 берётся из таблицы 2.6 РЭ.\nДля расчёта погрешности также укажите уровень AC с панели.\nДля самодельного устройства погрешность по этой таблице не нормирована.')
        for title, widget in [('Частота / начало, кГц', self.frequency), ('Конец серии, кГц', self.end_frequency),
                ('Точек в серии', self.count), ('Ряд частот', self.spacing), ('Диапазон |Z|', self.measurement_range),
                ('Скорость', self.speed), ('Установление, с', self.settling), ('КУ / приспособление', self.fixture),
                ('Уровень AC с панели, В', self.signal_level)]:
            form.addRow(title, widget)
        self.action_buttons = [self.connection_button]
        for label, operation, hint in [
                ('Считать текущую точку', 'read', 'Считывает текущий результат и сохраняет его.\nЧастота, диапазон и скорость на панели прибора сохраняются.'),
                ('Измерить на частоте', 'point', 'Задаёт частоту из поля «Частота / начало», диапазон и скорость.\nПосле установления считывает и сохраняет новую точку.'),
                ('Снять частотную серию', 'series', 'Измеряет ряд от начальной до конечной частоты.\nОтключите частотный анализ на панели прибора. Каждая точка сразу сохраняется в БД.')]:
            button = QtWidgets.QPushButton(label)
            button.setToolTip(hint)
            button.clicked.connect(lambda checked=False, action=operation: self.launch(action))
            self.action_buttons.append(button)
            form.addRow(button)
        self.stop_button = QtWidgets.QPushButton('СТОП')
        self.stop_button.setToolTip('Останавливает серию после текущего обмена.\nСохранённые результаты остаются в архиве. Настройки на приборе остаются последними заданными.')
        self.stop_button.clicked.connect(self.stop)
        self.stop_button.setEnabled(False)
        form.addRow(self.stop_button)
        compact_inputs(controls)
        self.port.setMinimumWidth(100)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(controls)
        scroll.setMinimumWidth(330)
        scroll.setMaximumWidth(440)
        main.addWidget(scroll)
        results = QtWidgets.QWidget()
        results_layout = QtWidgets.QVBoxLayout(results)
        plot_row = QtWidgets.QHBoxLayout()
        self.parameter = QtWidgets.QComboBox()
        for key, (label, unit) in PARAMETERS.items():
            self.parameter.addItem(label + (f', {unit}' if unit else ''), key)
        self.parameter.currentIndexChanged.connect(self.redraw)
        plot_row.addWidget(self.parameter)
        reset = QtWidgets.QPushButton('Сброс масштаба')
        reset.setToolTip('Показывает все измеренные точки на графике.')
        reset.clicked.connect(lambda: self.plot.enableAutoRange())
        plot_row.addWidget(reset)
        plot_row.addStretch()
        results_layout.addLayout(plot_row)
        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.plot = pg.PlotWidget(background='#18212d')
        self.plot.setMenuEnabled(False)
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.hover = PointHover(self.plot, self.point_hint)
        self.splitter.addWidget(self.plot)
        self.table = QtWidgets.QTableWidget()
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        configure_table(self.table)
        self.result_tabs = QtWidgets.QTabWidget()
        self.result_tabs.addTab(self.table, 'Параметры')
        self.uncertainty_table = QtWidgets.QTableWidget()
        self.uncertainty_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        configure_table(self.uncertainty_table)
        self.uncertainty_header_hints = HeaderHints(self.uncertainty_table.horizontalHeader())
        self.result_tabs.addTab(self.uncertainty_table, 'Погрешности выбранного параметра')
        self.splitter.addWidget(self.result_tabs)
        self.splitter.setSizes([300, 220])
        results_layout.addWidget(self.splitter)
        main.addWidget(results, 1)
        archive = QtWidgets.QWidget()
        archive_layout = QtWidgets.QVBoxLayout(archive)
        self.tabs.addTab(archive, 'Архив LCR')
        row = QtWidgets.QHBoxLayout()
        for label, callback in [('Обновить', self.refresh_archive), ('Сбросить фильтры', self.clear_filters),
                ('Погрешности…', self.recalculate_uncertainty), ('Экспорт CSV', self.export_csv), ('Экспорт JSON', self.export_json)]:
            button = QtWidgets.QPushButton(label)
            button.clicked.connect(callback)
            button.setToolTip({'Обновить': 'Обновляет список серий в выбранной базе LCR.',
                'Сбросить фильтры': 'Показывает все серии. Для выбора значений нажмите заголовок столбца.',
                'Погрешности…': 'Задаёт КУ и AC-уровень для выбранной серии и пересчитывает погрешности.\nПовторное измерение не проводится; исходные пакеты сохраняются.',
                'Экспорт CSV': 'Сохраняет точки выбранной серии в CSV для Excel.',
                'Экспорт JSON': 'Сохраняет серию с условиями и исходными ответами прибора.'}[label])
            row.addWidget(button)
        row.addStretch()
        archive_layout.addLayout(row)
        self.archive = QtWidgets.QTableWidget(0, 7)
        self.archive.setHorizontalHeaderLabels(['Дата ▾', 'Образец ▾', 'Маркировка ▾', 'Партия ▾', 'Статус ▾', 'Точек ▾', 'Ошибка ▾'])
        self.archive.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.archive.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.archive.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.archive.setHorizontalHeader(FilterHeader(self.archive))
        self.archive.horizontalHeader().sectionClicked.connect(self.filter_column)
        self.archive.itemSelectionChanged.connect(self.load_selected)
        configure_table(self.archive)
        archive_layout.addWidget(self.archive)
        self.archive_details = QtWidgets.QLabel('Выберите серию для просмотра условий и результатов.')
        self.archive_details.setWordWrap(True)
        self.archive_details.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        archive_layout.addWidget(self.archive_details)
        self.journal = QtWidgets.QPlainTextEdit()
        self.journal.setReadOnly(True)
        self.journal.setMaximumBlockCount(3000)
        self.tabs.addTab(self.journal, 'Журнал Е7-29')
        self.status = QtWidgets.QLabel('Е7-29 · USB / COM9 · 9600, 8N1')
        self.status.setWordWrap(True)
        self.status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        outer.addWidget(self.status)
        self.fields = dict(port=self.port, demo=self.demo, sample=self.sample, model=self.model,
            batch=self.batch, notes=self.notes, frequency=self.frequency, end_frequency=self.end_frequency,
            count=self.count, spacing=self.spacing, range=self.measurement_range, speed=self.speed,
            settling=self.settling, signal_level=self.signal_level, fixture=self.fixture, parameter=self.parameter)

    @staticmethod
    def number(low, high, value, decimals):
        widget = QtWidgets.QDoubleSpinBox()
        widget.setDecimals(decimals)
        widget.setRange(low, high)
        widget.setValue(value)
        return widget

    def settings(self):
        result = {}
        for key, widget in self.fields.items():
            result[key] = (widget.isChecked() if isinstance(widget, QtWidgets.QCheckBox) else
                widget.currentText() if key == 'port' else
                widget.currentIndex() if isinstance(widget, QtWidgets.QComboBox) else
                widget.value() if isinstance(widget, QtWidgets.QAbstractSpinBox) else widget.text())
        result['splitter'] = self.splitter.sizes()
        result['result_tab'] = self.result_tabs.currentIndex()
        result['tab'] = self.tabs.currentIndex()
        result['filters'] = {str(column): sorted(values) for column, values in self.filters.items()}
        return result

    def restore(self, settings):
        for key, widget in self.fields.items():
            if key not in settings:
                continue
            value = settings[key]
            if isinstance(widget, QtWidgets.QCheckBox):
                widget.setChecked(bool(value))
            elif key == 'port':
                index = widget.findText(str(value))
                if index >= 0:
                    widget.setCurrentIndex(index)
                else:
                    widget.setEditText(str(value))
            elif isinstance(widget, QtWidgets.QComboBox):
                if type(value) is int and 0 <= value < widget.count():
                    widget.setCurrentIndex(value)
            elif isinstance(widget, QtWidgets.QAbstractSpinBox):
                if isinstance(value, (int, float)):
                    widget.setValue(value)
            else:
                widget.setText(str(value))
        sizes = settings.get('splitter')
        if isinstance(sizes, list) and len(sizes) == 2 and all(type(size) is int and size >= 0 for size in sizes):
            self.splitter.setSizes(sizes)
        for key, widget in [('result_tab', self.result_tabs), ('tab', self.tabs)]:
            index = settings.get(key)
            if type(index) is int and 0 <= index < widget.count():
                widget.setCurrentIndex(index)
        filters = settings.get('filters', {})
        if isinstance(filters, dict):
            self.filters = {int(column): set(values) for column, values in filters.items()
                if column.isdigit() and 0 <= int(column) < 7 and isinstance(values, list)
                and all(isinstance(value, str) for value in values)}

    def changed(self, *args):
        self.settings_changed.emit(self.settings())
        if self.sender() is self.demo:
            self.clear_filters()

    def database_path(self):
        return self.root / 'data' / ('rlc-demo.db' if self.demo.isChecked() else 'rlc.db')

    def error(self, message):
        self.status.setStyleSheet('color:#ffaf9d; background:#482d32; padding:6px')
        self.status.setText('ОШИБКА: ' + message)
        self.journal.appendPlainText('ОШИБКА: ' + message)

    def launch(self, operation):
        if self.worker is not None:
            return
        for widget in self.fields.values():
            widget.setStyleSheet('')
        config = self.settings()
        if not config['port'].strip() and not config['demo']:
            self.port.setStyleSheet('background:#65383c; border:1px solid #ffaf9d')
            self.error('Укажите COM-порт Е7-29')
            return
        config['port'] = config['port'].strip()
        if operation not in ('identify', 'read') and operation == 'series' and config['end_frequency'] <= config['frequency']:
            self.end_frequency.setStyleSheet('background:#65383c; border:1px solid #ffaf9d')
            self.error('Конечная частота должна быть больше начальной')
            return
        if config['signal_level'].strip() and operation != 'identify':
            try:
                value = float(config['signal_level'].replace(',', '.'))
                if not math.isfinite(value) or not 0.04 <= value <= 1:
                    raise ValueError()
                config['operator_ac_level_v'] = value
            except ValueError:
                self.signal_level.setStyleSheet('background:#65383c; border:1px solid #ffaf9d')
                self.error('Уровень с панели: 0,04…1 В или пустое поле')
                return
        start, end = round(config['frequency'] * 1000), round(config['end_frequency'] * 1000)
        config['frequencies'] = ([round(start * (end/start) ** (index/(config['count']-1)))
            if config['spacing'] == 0 else round(start + (end-start)*index/(config['count']-1))
            for index in range(config['count'])] if operation == 'series' else [start])
        config.update(range_index=config['range']-1, settling_s=config['settling'], operation=operation,
                      fixture_id=self.fixture.currentData())
        self.status.setStyleSheet('')
        if operation != 'identify':
            self.points = []
            self.redraw()
        self.worker = RlcWorker(self, config, operation, self.database_path())
        self.worker.log.connect(self.journal.appendPlainText)
        self.worker.status.connect(self.status.setText)
        self.worker.failed.connect(self.error)
        self.worker.point.connect(self.receive)
        self.worker.finished.connect(self.worker_finished)
        if operation == 'identify':
            self.connection_button.watch(self.worker, config['demo'])
        for widget in list(self.fields.values()) + self.action_buttons:
            widget.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.worker.start()

    def stop(self):
        if self.worker is not None:
            self.worker.stop.set()
            self.status.setText('Остановка · ожидание завершения обмена…')

    def worker_finished(self):
        worker, self.worker = self.worker, None
        worker.deleteLater()
        for widget in list(self.fields.values()) + self.action_buttons:
            widget.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.refresh_archive()

    def receive(self, point):
        self.points.append(point)
        self.redraw()

    @staticmethod
    def value_text(value):
        return '—' if value is None else f'{value:.7g}'

    def redraw(self, *args):
        if not hasattr(self, 'plot'):
            return
        key = self.parameter.currentData()
        label, unit = PARAMETERS[key]
        self.hover.clear()
        self.plot.clear()
        self.hover.restore_lines()
        self.plot.setLabel('bottom', 'Частота', units='Hz')
        self.plot.setLabel('left', label, units=unit)
        self.plot.setTitle('ДЕМО' if self.points and self.points[0].get('demo') else 'Е7-29')
        valid = sorted((point for point in self.points if point['values'].get(key) is not None),
                       key=lambda point: point['frequency_hz'])
        if valid:
            x = [point['frequency_hz'] for point in valid]
            y = [point['values'][key] for point in valid]
            self.plot.getViewBox().setLimits(minYRange=max(max(abs(value) for value in y) * 1e-5, 1e-15))
            curve = self.plot.plot(x, y, data=valid,
                pen=pg.mkPen('#68d2c0', width=2), symbol='o', symbolSize=8)
            self.hover.attach(curve, x, y, valid)
        keys = list(PARAMETERS)
        headers = ['№', 'Частота, Гц'] + [name + (f', {units}' if units else '') for name, units in PARAMETERS.values()] + ['DC, В', 'Схема', 'Статус']
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setRowCount(len(self.points))
        for row, point in enumerate(self.points):
            values = [str(point['position']), str(point['frequency_hz'])] + [self.value_text(point['values'].get(key)) for key in keys]
            values += [self.value_text(point['bias_v']), 'Параллельная' if point['equivalent_circuit'] == 'parallel' else 'Последовательная',
                       'Перегрузка' if point['overloaded'] else 'Измерено · R<0' if point['values'].get('rs_ohm', 0) < 0 else 'Измерено']
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                if point['overloaded'] or point['values'].get('rs_ohm', 0) < 0:
                    item.setBackground(QtGui.QColor('#65383c'))
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()
        self.uncertainty_table.setColumnCount(8)
        self.uncertainty_table.setHorizontalHeaderLabels(['№', 'Частота, Гц', f'{label}, {unit}',
            f'±Δ, {unit}', 'δ, %', f'uB, {unit}', f'U (k=2), {unit}', 'Основание / причина'])
        header_hints = [
            'Номер точки в измеренной серии.\nСохраняется при загрузке из архива и не зависит от выбранного параметра.',
            'Фактическая частота испытательного сигнала, Гц.\nСчитана из ответа Е7-29. Именно она используется для пересчёта импеданса и расчёта погрешности.',
            f'Значение выбранного параметра {label}' + (f', {unit}.' if unit else '.') +
            '\nРассчитано из измеренных |Z| и фазы с учётом частоты.\nВыбор параметра над графиком одновременно изменяет эту таблицу. «—» означает неприменимое или отсутствующее значение.',
            'Δ — предел допускаемой основной абсолютной погрешности по РЭ Е7-29, в единицах выбранного параметра.\n'
            'Запись ±Δ задаёт границы допускаемой ошибки. Это не измеренная систематическая ошибка и не поправка.\n'
            'Расчёт: таблицы 2.2–2.6 РЭ; учитываются частота, |Z|, фактический диапазон, скорость, уровень AC и подключённое КУ.\n'
            'Предполагаются нормальные условия и прогрев не менее 15 минут.',
            'δ — предел основной относительной погрешности, %.\n'
            'Для параметров с нормированной относительной погрешностью: δ = 100·Δ/|значение|.\n'
            'Для фазы, Q и D РЭ задаёт абсолютный предел; в этом столбце показано «—».\n'
            'Значение берётся из формул РЭ, а не из разброса повторных измерений.',
            'uB — стандартная неопределённость типа B по характеристикам из РЭ.\n'
            'u — стандартная неопределённость; индекс B здесь обозначает способ оценки по имеющимся сведениям. '
            'Тип A оценивается по статистике повторных измерений.\n'
            'В программе принято равномерное распределение внутри ±Δ: uB = Δ/√3. '
            'Это принятое допущение расчёта, а не готовая характеристика изготовителя.\n'
            'Метод: GUM, JCGM 100:2008, раздел 4.3. Единицы совпадают с единицами параметра.',
            'U — расширенная неопределённость по РЭ при коэффициенте охвата k=2.\n'
            'В GUM: U = k·uc, где uc — суммарная стандартная неопределённость. '
            'Сейчас в программе учтена только составляющая типа B: U = 2·uB = 2·Δ/√3.\n'
            'Это не полная неопределённость опыта: повторяемость (тип A), дополнительная температурная '
            'погрешность и неизвестные остаточные параметры оснастки не оценены.\n'
            'Для принятого равномерного распределения k=2 нельзя автоматически трактовать как вероятность 95 %.\n'
            'Обозначение U и коэффициент k — GUM, JCGM 100:2008, раздел 6; в РЭ Е7-29 заданы δ и Δ.',
            'Основание расчёта или причина его отсутствия.\n'
            '«Основная · РЭ · тип B» — оценка по таблицам 2.2–2.6 РЭ Е7-29 с принятым равномерным распределением.\n'
            'Если не заданы КУ или AC-уровень, превышены нормированные условия, есть перегрузка '
            'или отрицательная активная часть импеданса, вместо числа сохраняется причина.\n'
            'Для архивной серии условия можно задать кнопкой «Погрешности…» без повторных измерений.',
        ]
        for column, hint in enumerate(header_hints):
            self.uncertainty_table.horizontalHeaderItem(column).setToolTip(hint)
        self.uncertainty_table.setRowCount(len(self.points))
        for row, point in enumerate(self.points):
            uncertainty = point.get('uncertainty', {})
            parameter = uncertainty.get('parameters', {}).get(key, {})
            reason = ('Основная · РЭ · тип B' if parameter else
                      uncertainty.get('reason', 'Для параметра нет расчёта; задайте КУ и AC-уровень'))
            cells = [str(point['position']), str(point['frequency_hz']), self.value_text(point['values'].get(key)),
                self.value_text(parameter.get('error_limit_abs')), self.value_text(parameter.get('error_limit_percent')),
                self.value_text(parameter.get('standard_uncertainty_abs')),
                self.value_text(parameter.get('expanded_uncertainty_k2_abs')), reason]
            for column, value in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(value)
                if not parameter:
                    item.setBackground(QtGui.QColor('#513b32'))
                self.uncertainty_table.setItem(row, column, item)
        self.uncertainty_table.resizeColumnsToContents()
        self.uncertainty_table.setColumnWidth(7, 300)
        self.uncertainty_table.resizeRowsToContents()

    def point_hint(self, point):
        key = self.parameter.currentData()
        label, unit = PARAMETERS[key]
        uncertainty = point.get('uncertainty', {})
        parameter = uncertainty.get('parameters', {}).get(key, {})
        error_text = (f"\nПредел погрешности: ±{self.value_text(parameter['error_limit_abs'])} {unit}"
            f"\nuB: {self.value_text(parameter['standard_uncertainty_abs'])} {unit}"
            f"\nU (k=2): {self.value_text(parameter['expanded_uncertainty_k2_abs'])} {unit}"
            if parameter else '\nПогрешность: ' + uncertainty.get('reason', 'нет расчёта'))
        return (f"Точка {point['position']} из {len(self.points)}\nЧастота: {point['frequency_hz']:g} Гц\n"
                f"{label}: {self.value_text(point['values'].get(key))} {unit}\n"
                f"|Z|: {self.value_text(point['values'].get('z_ohm'))} Ом\n"
                f"Фаза: {self.value_text(point['values'].get('phase_deg'))}°" + error_text)

    def refresh_archive(self):
        try:
            self.archive_rows = RlcStore(self.database_path()).series()
            self.archive.setRowCount(0)
            for series in self.archive_rows:
                config = series['settings']
                values = [datetime.fromisoformat(series['created_at']).astimezone().strftime('%d.%m.%Y %H:%M:%S'),
                    config.get('sample', ''), config.get('model', ''), config.get('batch', ''),
                    STATES.get(series['status'], series['status']), str(series['count']), series['error']]
                row = self.archive.rowCount()
                self.archive.insertRow(row)
                for column, value in enumerate(values):
                    item = QtWidgets.QTableWidgetItem(value)
                    item.setData(QtCore.Qt.ItemDataRole.UserRole, series['id'])
                    if series['status'] == 'failed':
                        item.setBackground(QtGui.QColor('#65383c'))
                    self.archive.setItem(row, column, item)
                self.archive.setRowHidden(row, any(values[column] not in allowed for column, allowed in self.filters.items()))
            self.archive.horizontalHeader().set_active_filters(self.filters)
            self.archive.resizeColumnsToContents()
        except Exception as error:
            self.error(str(error))

    def clear_filters(self):
        self.filters = {}
        self.refresh_archive()
        self.changed()

    def filter_column(self, column):
        from .ui import ColumnFilterDialog
        values = sorted({self.archive.item(row, column).text() for row in range(self.archive.rowCount())})
        dialog = ColumnFilterDialog(self.archive.horizontalHeaderItem(column).text(), values, self.filters.get(column), self)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            selected = dialog.selected_values()
            if set(selected) == set(values):
                self.filters.pop(column, None)
            else:
                self.filters[column] = set(selected)
            self.refresh_archive()
            self.changed()

    def selected_series(self):
        selected = self.archive.selectedItems()
        identifier = selected[0].data(QtCore.Qt.ItemDataRole.UserRole) if selected else None
        return next((series for series in self.archive_rows if series['id'] == identifier), None)

    def load_selected(self):
        if self.worker is not None:
            return
        series = self.selected_series()
        if series:
            try:
                self.points = RlcStore(self.database_path()).points(series['id'])
                self.redraw()
                config = series['settings']
                ac_level = config.get('operator_ac_level_v')
                self.archive_details.setText(
                    f"{config.get('identity', 'Е7-29')} · образец: {config.get('sample', '')}\n"
                    f"Маркировка: {config.get('model', '')} · партия: {config.get('batch', '')}\n"
                    f"Уровень AC с панели: {self.value_text(ac_level)} В · "
                    f"КУ: {dict((identifier, name) for name, identifier in FIXTURES).get(config.get('fixture_id'), 'Не указано')} · "
                    f"порт: {config.get('port', '')}\nПримечание: {config.get('notes', '')}")
                self.status.setText('Архив · ' + series['settings'].get('sample', '') + ' · ' + str(self.database_path()))
            except Exception as error:
                self.error(str(error))

    def export(self, kind):
        series = self.selected_series()
        if series is None:
            self.error('Выберите серию в архиве')
            return
        filename, _ = QtWidgets.QFileDialog.getSaveFileName(self, 'Экспорт LCR', f'e729-{series["id"]}.{kind}', f'{kind.upper()} (*.{kind})')
        if not filename:
            return
        try:
            points = RlcStore(self.database_path()).points(series['id'])
            if kind == 'json':
                with open(filename, 'w', encoding='utf-8') as output:
                    json.dump(dict(**series, points=points), output, ensure_ascii=False, indent=2, allow_nan=False)
            else:
                with open(filename, 'w', encoding='utf-8-sig', newline='') as output:
                    writer = csv.writer(output, delimiter=';')
                    columns = ['position', 'frequency_hz', 'measured_at', 'bias_v', 'status']
                    error_fields = ['error_limit_abs', 'error_limit_percent', 'standard_uncertainty_abs', 'expanded_uncertainty_k2_abs']
                    writer.writerow(columns + list(PARAMETERS) + ['fixture_id', 'operator_ac_level_v', 'uncertainty_reason'] +
                        [key + '_' + field for key in PARAMETERS for field in error_fields])
                    for point in points:
                        uncertainty = point.get('uncertainty', {})
                        writer.writerow([point.get(key) for key in columns] + [point['values'].get(key) for key in PARAMETERS] +
                            [uncertainty.get('fixture_id'), uncertainty.get('operator_ac_level_v'), uncertainty.get('reason', '')] +
                            [uncertainty.get('parameters', {}).get(key, {}).get(field) for key in PARAMETERS for field in error_fields])
            self.status.setText('Сохранено: ' + filename)
        except Exception as error:
            self.error(str(error))

    def export_csv(self):
        self.export('csv')

    def export_json(self):
        self.export('json')

    def recalculate_uncertainty(self):
        if self.worker is not None:
            self.error('Дождитесь завершения измерений')
            return
        series = self.selected_series()
        if series is None:
            self.error('Выберите серию в архиве')
            return
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle('Погрешности · условия выбранной серии')
        form = QtWidgets.QFormLayout(dialog)
        fixture = QtWidgets.QComboBox()
        for name, identifier in FIXTURES:
            if identifier in ('UP-2', 'UP-5', 'UP-9'):
                fixture.addItem(name, identifier)
        selected = fixture.findData(series['settings'].get('fixture_id', self.fixture.currentData()))
        fixture.setCurrentIndex(max(0, selected))
        level = self.number(.04, 1, series['settings'].get('operator_ac_level_v', 1) or 1, 3)
        form.addRow('КУ во время измерений', fixture)
        form.addRow('AC на панели при измерениях, В', level)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText('Пересчитать')
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText('Отмена')
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            try:
                RlcStore(self.database_path()).recalculate_uncertainty(series['id'], fixture.currentData(), level.value())
                self.refresh_archive()
                for row in range(self.archive.rowCount()):
                    if self.archive.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole) == series['id']:
                        self.archive.selectRow(row)
                        break
                self.status.setText('Погрешности пересчитаны и сохранены в БД')
            except Exception as error:
                self.error(str(error))
