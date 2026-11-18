"""Shared sizing for instrument settings, independent of the available panel width."""

from PySide6 import QtCore, QtWidgets


class GroupedFormRows:
    """Keep row visibility and labels addressable across grouped form layouts."""

    def __init__(self):
        self.rows = {}

    def register(self, widget, form):
        self.rows[widget] = form

    def setRowVisible(self, widget, visible):
        self.rows[widget].setRowVisible(widget, visible)

    def isRowVisible(self, widget):
        return self.rows[widget].isRowVisible(widget)

    def labelForField(self, widget):
        return self.rows[widget].labelForField(widget)


def compact_inputs(container):
    for form in container.findChildren(QtWidgets.QFormLayout):
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
    for widget in container.findChildren(QtWidgets.QWidget):
        if isinstance(widget, QtWidgets.QAbstractSpinBox):
            widget.setMaximumWidth(140)
        elif isinstance(widget, QtWidgets.QComboBox):
            widget.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents)
            widget.setMaximumWidth(280)
            for index in range(widget.count()):
                widget.setItemData(index, widget.itemText(index), QtCore.Qt.ItemDataRole.ToolTipRole)
        elif isinstance(widget, QtWidgets.QLineEdit) and not isinstance(widget.parent(), QtWidgets.QAbstractSpinBox):
            widget.setMaximumWidth(340)
            widget.setMinimumWidth(min(300, max(150, widget.fontMetrics().horizontalAdvance(widget.text()) + 24)))
        else:
            continue
        widget.setSizePolicy(QtWidgets.QSizePolicy.Policy.Maximum, QtWidgets.QSizePolicy.Policy.Fixed)
