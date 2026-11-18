"""Button help after three seconds inside a button, even while the mouse moves."""

import weakref

from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import isValid


BUTTON_HELP = {
    "Измерить": "Запускает измерение в выбранном рабочем режиме с текущими настройками.",
    "Измерить образец": "Снимает заданные точки характеристики образца и сохраняет измерение в выбранную базу.",
    "Измерить точку": "Измеряет напряжение детектора при начальном уровне мощности и сохраняет одну точку.",
    "Снять калибровочную кривую": "Проходит весь заданный ряд мощностей, измеряет напряжение детектора и сохраняет кривую.",
    "Измерить два тона": "Устанавливает два RF-тона, измеряет спектр и параметры выбранного смесителя или PA.",
    "Выполнить калибровку уровней": "Измеряет каналы A/B на всех заданных частотах и уровнях и сохраняет поправки. Измеритель должен быть подключён вместо DUT.",
    "Измерить один раз": "Применяет текущие настройки и выполняет одну новую развёртку RSA.",
    "Запустить серию": "Выполняет заданную последовательность измерений с сохранением результатов каждой точки.",
    "Считать трассу": "Считывает уже имеющийся спектр RSA, не меняя настройки и состояния генераторов.",
    "Связь / состояние SMU": "Подключается к SMU200A и считывает паспорт и состояние каналов без включения RF.",
    "Связь / состояние АКИП": "Подключается к АКИП-3208 и считывает его паспорт и текущее состояние.",
    "Применить CW · выходы OFF": "Настраивает частоты и уровни SMU в режиме CW. Оба выхода остаются выключенными.",
    "Включить по схеме измерения": "Включает RF-выходы, необходимые для выбранной схемы измерения.",
    "Отключить оба выхода SMU": "Отправляет RF OFF обоим каналам SMU и проверяет, что они отключены.",
    "Применить настройки RSA": "Передаёт выбранные параметры развёртки анализатору и проверяет применённые значения.",
    "Предустановка RSA для двух тонов": "Заполняет настройки RSA для разделения двух тонов и продуктов IM3. Приборы этой кнопкой не переключаются.",
    "Сбросить масштаб": "Возвращает обе оси графика к диапазону всех отображаемых измеренных точек.",
    "Масштаб по данным": "Возвращает обе оси графика к диапазону отображаемых данных.",
    "Кривая серии": "Строит зависимость результата от частоты или уровня в выбранной серии измерений.",
    "Мощность и PEP": "Показывает среднюю мощность и пиковую мощность огибающей двух основных тонов.",
    "IM3 и гармоники": "Показывает измеренные продукты IM3 и гармонические полосы по точкам серии.",
    "Из архива": "Загружает сохранённые двухтоновые результаты для просмотра и построения графиков.",
    "Обновить": "Перечитывает записи из базы и обновляет таблицу с учётом текущих фильтров.",
    "Обновить профили": "Перечитывает сохранённые калибровочные профили и обновляет их список.",
    "Пересчитать в dBm": "Определяет мощность по введённому напряжению и выбранной калибровочной кривой детектора.",
    "Выбрать всё": "Отмечает все значения фильтра, включая скрытые поиском.",
    "Снять всё": "Снимает отметки со всех значений фильтра.",
    "Сбросить фильтры": "Снимает ограничения столбцов и снова показывает все подходящие записи.",
    "Построить": "Строит график по выбранным данным и параметрам осей.",
    "Применить": "Применяет выбранные параметры и закрывает окно настройки.",
    "Отмена": "Закрывает окно без применения внесённых изменений.",
    "Закрыть": "Закрывает это окно и возвращает к основному рабочему окну.",
    "Восстановить": "Проверяет сохранённые промежуточные результаты и восстанавливает их в базу.",
    "Выбрать БД": "Открывает выбор существующего файла базы измерений.",
    "Создать БД": "Создаёт новый файл базы измерений по указанному пути.",
    "Выбрать SQLite-базу": "Открывает существующую SQLite-базу для сохранения и просмотра измерений компонентов.",
    "Создать новую базу": "Создаёт SQLite-базу измерений компонентов в выбранной папке.",
    "Сохранить настройки ПК": "Сохраняет текущие настройки программы для следующего запуска на этом компьютере.",
    "Импорт измерений JSON": "Загружает сохранённые измерения компонентов из JSON в текущую базу.",
    "Экспорт базы в JSON": "Сохраняет измерения базы в JSON для резервной копии или переноса на другой компьютер.",
    "Восстановить несохранённые": "Восстанавливает измерения из промежуточных файлов после сбоя записи в базу.",
    "Задать точки": "Рассчитывает линейный или логарифмический ряд токов по заданным границам и числу точек.",
    "Подобрать выбранные": "Сравнивает выбранные образцы по текущему методу подбора и допуску.",
    "СТОП / отключение выхода": "Прерывает измерение компонента и отключает выход источника-измерителя с проверкой состояния.",
}


def hint_text(button):
    if button.toolTip():
        return button.toolTip()
    text = button.text().replace("&", "").replace("…", "").lstrip("▶■ ").strip().rstrip(".")
    if text in BUTTON_HELP:
        return BUTTON_HELP[text]
    if text.startswith("СТОП"):
        return "Прерывает текущий прогон и отключает управляемые RF-выходы с проверкой состояния OFF."
    if text.startswith("Проверить"):
        return "Проверяет связь и паспорт выбранного прибора. RF-выход генератора не включается."
    if text.startswith("CSV") or "Экспорт CSV" in text:
        return "Сохраняет измеренные точки или выбранную сводку в CSV для обработки в таблицах."
    if text.startswith("PNG"):
        return "Сохраняет текущий график в файл PNG."
    if "Экспорт JSON" in text:
        return "Сохраняет измерения и условия в JSON для переноса на другой компьютер."
    if "Импорт JSON" in text:
        return "Загружает измерения из JSON в выбранную базу с проверкой идентификаторов записей."
    if text.startswith("Взять частоты"):
        return "Копирует частоты и ряд уровней из двухтонового режима в процедуру калибровки. Измерение не запускается."
    if text.startswith("Поменять оси"):
        return "Меняет местами оси напряжения и тока без изменения сохранённых измерений."
    if text.startswith("Устойчивое измерение"):
        return "Заполняет RMS-детектор, RBW/VBW 100 Гц и пять развёрток на точку для стабильного измерения CW."
    if text.startswith("Обзор вокруг"):
        return "Центрирует обзор RSA на ожидаемой частоте измеряемого сигнала."
    if isinstance(button, QtWidgets.QToolButton) and not text:
        return {QtCore.Qt.ArrowType.LeftArrow: "Прокрутить вкладки влево.",
            QtCore.Qt.ArrowType.RightArrow: "Прокрутить вкладки вправо."}.get(button.arrowType(), button.accessibleName())
    return button.accessibleDescription() or text or button.accessibleName()


class ButtonHints(QtCore.QObject):
    def __init__(self, app):
        super().__init__(app)
        self.hovered = None
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
        self.timer.setInterval(3000)
        self.timer.timeout.connect(self.show_hint)
        self.label = QtWidgets.QLabel(None, QtCore.Qt.WindowType.ToolTip | QtCore.Qt.WindowType.WindowTransparentForInput)
        self.label.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.label.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.label.setWordWrap(True)
        self.label.setMaximumWidth(360)
        self.label.setStyleSheet("""
            QLabel
            {
                background: #263447;
                color: #f5f8fc;
                border: 1px solid #8191a8;
                border-radius: 4px;
                padding: 7px;
                font-family: 'Segoe UI';
                font-size: 10pt;
            }
        """)

    def hide_hint(self):
        if isValid(self.timer):
            self.timer.stop()
        self.hovered = None
        if isValid(self.label):
            self.label.hide()

    def show_hint(self):
        button = self.hovered() if self.hovered else None
        if button is None or not isValid(button) or not isValid(self.label) or not button.isVisible() or not button.underMouse():
            return
        text = hint_text(button)
        if not text:
            return
        self.label.setText(text)
        self.label.adjustSize()
        screen = button.screen().availableGeometry()
        position = button.mapToGlobal(QtCore.QPoint(0, button.height() + 6))
        if position.y() + self.label.height() > screen.bottom():
            position.setY(button.mapToGlobal(QtCore.QPoint(0, -self.label.height() - 6)).y())
        position.setX(max(screen.left() + 4, min(position.x(), screen.right() - self.label.width() - 4)))
        position.setY(max(screen.top() + 4, min(position.y(), screen.bottom() - self.label.height() - 4)))
        self.label.move(position)
        self.label.show()

    def eventFilter(self, watched, event):
        if isinstance(watched, QtWidgets.QAbstractButton):
            kind = event.type()
            if kind == QtCore.QEvent.Type.Enter:
                self.hide_hint()
                self.hovered = weakref.ref(watched)
                self.timer.start()
            elif kind in (QtCore.QEvent.Type.Leave, QtCore.QEvent.Type.Hide, QtCore.QEvent.Type.MouseButtonPress):
                if self.hovered and self.hovered() is watched:
                    self.hide_hint()
            elif kind == QtCore.QEvent.Type.MouseButtonRelease and watched.rect().contains(event.position().toPoint()):
                self.hide_hint()
                self.hovered = weakref.ref(watched)
                self.timer.start()
            elif kind == QtCore.QEvent.Type.ToolTip:
                return True
        elif event.type() == QtCore.QEvent.Type.WindowDeactivate and isinstance(watched, QtWidgets.QWidget):
            button = self.hovered() if self.hovered else None
            if button is not None and isValid(button) and isValid(watched) and watched is button.window():
                self.hide_hint()
        return super().eventFilter(watched, event)


def install_button_hints():
    app = QtWidgets.QApplication.instance()
    if app is not None and not hasattr(app, "button_hints"):
        app.button_hints = ButtonHints(app)
        app.installEventFilter(app.button_hints)
