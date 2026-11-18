"""Visible status messages and field-specific validation shared by RF forms."""

import re

from PySide6 import QtCore, QtWidgets


class FieldError(ValueError):
    def __init__(self, message, *fields):
        super().__init__(message)
        self.fields = fields


class StatusLabel(QtWidgets.QLabel):
    def __init__(self, text=""):
        super().__init__()
        self.setWordWrap(True)
        self.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.setText(text)

    def setText(self, text):
        error = any(word in text.upper() for word in ("ОШИБК", "НЕ ПОДТВЕРЖД", "FAILED", "COMPLETE_WITH_GAPS"))
        error = error or bool(re.search(r"ПРОПУЩЕНО\s+[1-9]", text.upper()))
        self.setProperty("hasError", error)
        self.setStyleSheet("""
            QLabel
            {
                background: #612c34;
                color: #ffffff;
                border: 2px solid #ff808c;
                border-radius: 4px;
                padding: 7px;
                font-weight: bold;
            }
        """ if error else "")
        super().setText(text)


class FieldFeedback:
    def __init__(self, fields):
        self.fields = fields
        self.marked = {}
        for widget in set(fields.values()):
            signal = (widget.currentIndexChanged if isinstance(widget, QtWidgets.QComboBox) else
                widget.toggled if isinstance(widget, QtWidgets.QCheckBox) else
                widget.valueChanged if isinstance(widget, QtWidgets.QAbstractSpinBox) else
                widget.textChanged if isinstance(widget, QtWidgets.QLineEdit) else None)
            if signal is not None:
                signal.connect(lambda *args, widget=widget: self.clear_widget(widget))

    def clear_widget(self, widget):
        previous = self.marked.pop(widget, None)
        if previous is not None:
            widget.setProperty("inputError", False)
            widget.setStyleSheet(previous[0])
            widget.setToolTip(previous[1])

    def clear(self):
        for widget in list(self.marked):
            self.clear_widget(widget)

    def show(self, error, keys=None, reveal=True):
        first = None
        if keys is None:
            keys = getattr(error, "fields", ())
            text = str(error).upper()
            if not keys and any(token in text for token in ("TIMEOUT", "TIMED OUT", "CONNECT", "VISA", "ОТВЕТ", "СОЕДИН", "СВЯЗ", "DSR")):
                for name, fields in (("NRP", ("nrp_resource",)), ("DMM6500", ("dmm_host", "dmm_port")),
                        ("34401", ("com_port", "baud")), ("SMU", ("smu_host", "smu_port")),
                        ("RSA", ("host",)), ("АКИП", ("akip_host", "akip_port"))):
                    if name in text:
                        keys = fields
                        break
        for key in keys:
            widget = self.fields.get(key)
            if widget is None:
                continue
            if widget not in self.marked:
                self.marked[widget] = (widget.styleSheet(), widget.toolTip())
            previous_style, previous_tip = self.marked[widget]
            widget.setProperty("inputError", True)
            widget.setStyleSheet(previous_style + """
                QLineEdit, QAbstractSpinBox, QComboBox, QCheckBox
                {
                    background: #612c34;
                    color: #ffffff;
                    border: 2px solid #ff808c;
                    border-radius: 3px;
                }
                QAbstractSpinBox QLineEdit
                {
                    background: #612c34;
                    color: #ffffff;
                    border: none;
                }
            """)
            widget.setToolTip(str(error) + ("\n\n" + previous_tip if previous_tip else ""))
            if first is None:
                first = widget
        if first is not None and reveal:
            first.setFocus()
            parent = first.parentWidget()
            while parent is not None:
                if isinstance(parent, QtWidgets.QScrollArea):
                    parent.ensureWidgetVisible(first, 16, 16)
                    break
                parent = parent.parentWidget()
