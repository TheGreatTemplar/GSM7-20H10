import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import threading
import unittest

from PySide6 import QtCore, QtWidgets

from gsmbench.instrument_buttons import connection_test_button


class CheckWorker(QtCore.QObject):
    failed = QtCore.Signal(str)
    finished = QtCore.Signal()

    def __init__(self):
        super().__init__()
        self.stop = threading.Event()


class ConnectionButtonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_failure_remains_red_after_worker_finishes(self):
        button = connection_test_button('Проверить связь', lambda: None)
        worker = CheckWorker()
        button.watch(worker)
        self.assertEqual(button.property('connectionState'), 'busy')
        worker.failed.emit('Нет ответа от COM9')
        worker.finished.emit()
        self.assertEqual(button.property('connectionState'), 'error')
        self.assertIn('Нет ответа от COM9', button.toolTip())
        self.assertIn('#ff877e', button.styleSheet())
        # A subsequent successful attempt must replace the previous error.
        worker2 = CheckWorker()
        button.watch(worker2)
        worker2.finished.emit()
        self.assertEqual(button.property('connectionState'), 'ok')
        self.assertNotIn('Нет ответа от COM9', button.toolTip())
        self.assertFalse(button.icon().isNull())
        button.close()

    def test_endpoint_change_invalidates_even_a_late_result(self):
        button = connection_test_button('Проверить связь', lambda: None)
        port = QtWidgets.QComboBox()
        port.setEditable(True)
        port.addItem('COM9')
        button.bind_endpoint(port)
        worker = CheckWorker()
        button.watch(worker)
        port.setEditText('COM10')
        self.assertEqual(button.property('connectionState'), 'idle')
        worker.finished.emit()
        self.assertEqual(button.property('connectionState'), 'idle')
        button.close()
        port.close()

    def test_demo_and_cancel_never_show_successful_real_connection(self):
        button = connection_test_button('Проверить связь', lambda: None)
        worker = CheckWorker()
        button.watch(worker, demo=True)
        worker.finished.emit()
        self.assertEqual(button.property('connectionState'), 'demo')
        self.assertIn('не проверялась', button.toolTip())
        worker2 = CheckWorker()
        button.watch(worker2)
        worker2.stop.set()
        worker2.finished.emit()
        self.assertEqual(button.property('connectionState'), 'idle')
        button.close()


if __name__ == '__main__':
    unittest.main()
