import unittest
from unittest.mock import patch

from PySide6 import QtCore, QtGui, QtWidgets, QtTest
from gsmbench.button_hints import ButtonHints


class FieldHintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_multiline_field_hint_persists_beyond_native_tooltip_timeout(self):
        editor = QtWidgets.QPlainTextEdit()
        editor.setToolTip('Частоты первого тона\n14; 14.175; 14.35\n50..52/1')
        editor.show()
        self.app.processEvents()
        hints = ButtonHints(self.app)
        inside = editor.mapToGlobal(editor.rect().center())
        with patch.object(QtGui.QCursor, 'pos', return_value=inside):
            hints.eventFilter(editor.viewport(), QtCore.QEvent(QtCore.QEvent.Type.Enter))
            self.assertEqual(hints.timer.interval(), 3000)
            self.assertTrue(hints.timer.isActive())
            hints.timer.stop()
            hints.show_hint()
            self.assertTrue(hints.label.isVisible())
            self.assertIn('\n50..52/1', hints.label.text())
            QtTest.QTest.qWait(5500)
            self.assertTrue(hints.label.isVisible())
            self.assertTrue(hints.eventFilter(editor.viewport(), QtCore.QEvent(QtCore.QEvent.Type.ToolTip)))
        outside = editor.mapToGlobal(QtCore.QPoint(-20, -20))
        with patch.object(QtGui.QCursor, 'pos', return_value=outside):
            hints.eventFilter(editor.viewport(), QtCore.QEvent(QtCore.QEvent.Type.Leave))
            self.assertFalse(hints.label.isVisible())
        hints.label.close()
        editor.close()
