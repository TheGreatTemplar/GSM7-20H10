import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import tempfile
import time
import unittest

from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from gsmbench.rlc_storage import RlcStore
from gsmbench.rlc_ui import RlcPanel
from unittest.mock import patch


class RlcUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def wait_worker(self, panel):
        deadline = time.monotonic() + 10
        while panel.worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertIsNone(panel.worker)

    def test_demo_series_archive_plot_and_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            panel = RlcPanel(None, Path(folder), dict(demo=True, sample='Тест', count=3,
                frequency=50, end_frequency=1000, parameter=5, fixture=2, signal_level='1'))
            panel.launch('series')
            self.wait_worker(panel)
            self.assertEqual(len(panel.points), 3)
            self.assertEqual(panel.table.rowCount(), 3)
            self.assertEqual(panel.archive.rowCount(), 1)
            self.assertEqual(len(panel.hover.scatters), 1)
            self.assertIn('Точка 1 из 3', panel.point_hint(panel.points[0]))
            self.assertAlmostEqual(panel.points[0]['values']['ls_h'], 10e-6)
            self.assertEqual(panel.points[0]['uncertainty']['status'], 'estimated')
            self.assertEqual(panel.uncertainty_table.rowCount(), 3)
            self.assertIn('U (k=2)', panel.point_hint(panel.points[0]))
            store = RlcStore(panel.database_path())
            self.assertEqual(store.series()[0]['status'], 'complete')
            self.assertEqual(store.series()[0]['settings']['sample'], 'Тест')
            self.assertTrue(panel.database_path().name.endswith('-demo.db'))
            self.assertFalse((Path(folder) / 'data/rlc.db').exists())
            panel.parameter.setCurrentIndex(0)
            self.assertEqual(panel.settings()['parameter'], 0)
            panel.archive.selectRow(0)
            self.app.processEvents()
            self.assertEqual(len(panel.points), 3)
            panel.close()

    def test_stop_preserves_partial_series_and_releases_controls(self):
        with tempfile.TemporaryDirectory() as folder:
            panel = RlcPanel(None, Path(folder), dict(demo=True, count=1000, frequency=50))
            panel.launch('series')
            deadline = time.monotonic() + 5
            while not panel.points and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            panel.stop()
            self.wait_worker(panel)
            self.assertGreater(len(panel.points), 0)
            self.assertLess(len(panel.points), 1000)
            self.assertEqual(RlcStore(panel.database_path()).series()[0]['status'], 'cancelled')
            self.assertTrue(panel.action_buttons[0].isEnabled())
            self.assertFalse(panel.stop_button.isEnabled())
            panel.close()

    def test_point_hover_survives_render_zoom_and_parameter_change(self):
        from gsmbench.e729 import decode_measurement
        from gsmbench.rlc_uncertainty import measurement_uncertainty
        from tests.test_e729 import packet
        with patch('gsmbench.rlc_ui.RlcStore') as store:
            store.return_value.series.return_value = []
            panel = RlcPanel(None, Path.cwd(), dict(parameter=0))
            panel.resize(1400, 850)
            panel.show()
            for index, frequency in enumerate((50000, 250000, 1000000)):
                point = decode_measurement(packet(frequency=frequency))
                point.update(position=index+1, demo=True)
                point['uncertainty'] = measurement_uncertainty(point, 'UP-5', 1)
                panel.points.append(point)
            panel.redraw()
            for parameter in (0, 5):
                panel.parameter.setCurrentIndex(parameter)
                panel.plot.enableAutoRange()
                QtTest.QTest.qWait(150)
                panel.plot.getViewBox().scaleBy((1.1, 1.1))
                QtTest.QTest.qWait(150)
                scatter = panel.hover.scatters[0]
                point = scatter.points()[1]
                self.assertIsNotNone(point.data())
                position = panel.plot.mapFromScene(scatter.mapToScene(point.pos()))
                position = panel.plot.viewport().mapFrom(panel.plot, position)
                QtTest.QTest.mouseMove(panel.plot.viewport(), QtCore.QPoint(2, 2))
                QtTest.QTest.mouseMove(panel.plot.viewport(), position)
                QtTest.QTest.qWait(50)
                self.assertTrue(panel.hover.label.isVisible(),
                    f'parameter={parameter}, point={position}, viewport={panel.plot.viewport().rect()}, '
                    f'range={panel.plot.getViewBox().viewRange()}, selection={panel.hover.selection}')
                self.assertIn('Точка 2 из 3', panel.hover.label.text())
                self.assertIn('Частота:', panel.hover.label.text())
                self.assertIn('Предел погрешности:', panel.hover.label.text())
                self.assertTrue(all(line.isVisible() for line in panel.hover.lines))
            QtTest.QTest.qWait(5500)
            self.assertTrue(panel.hover.label.isVisible())
            QtTest.QTest.mouseMove(panel.plot.viewport(), QtCore.QPoint(2, 2))
            self.app.processEvents()
            self.assertFalse(panel.hover.label.isVisible())
            panel.close()

    def test_invalid_range_blocks_launch(self):
        with tempfile.TemporaryDirectory() as folder:
            panel = RlcPanel(None, Path(folder), dict(demo=True, frequency=1000, end_frequency=50))
            panel.launch('series')
            self.assertIsNone(panel.worker)
            self.assertIn('ОШИБКА', panel.status.text())
            self.assertIn('#65383c', panel.end_frequency.styleSheet())
            panel.close()

    def test_uncertainty_header_help_is_specific_and_persistent(self):
        with patch('gsmbench.rlc_ui.RlcStore') as store:
            store.return_value.series.return_value = []
            panel = RlcPanel(None, Path.cwd(), dict(parameter=5))
            panel.resize(1500, 850)
            panel.show()
            panel.result_tabs.setCurrentIndex(1)
            panel.redraw()
            QtTest.QTest.qWait(100)
            table = panel.uncertainty_table
            tips = [table.horizontalHeaderItem(column).toolTip() for column in range(8)]
            self.assertEqual(len(set(tips)), 8)
            self.assertTrue(all(tips))
            self.assertIn('Ls', tips[2])
            self.assertIn('uB = Δ/√3', tips[5])
            self.assertIn('95 %', tips[6])
            self.assertIn('не полная', tips[6])
            header = table.horizontalHeader()
            position = QtCore.QPoint(header.sectionViewportPosition(6)+8, header.height()//2)
            hints = panel.uncertainty_header_hints
            inside = header.viewport().mapToGlobal(position)
            with patch.object(QtGui.QCursor, 'pos', return_value=inside):
                hints.track(position)
                self.assertEqual(hints.timer.interval(), 3000)
                timer_id = hints.timer.timerId()
                hints.track(position + QtCore.QPoint(2, 0))
                self.assertEqual(hints.timer.timerId(), timer_id)
                hints.timer.stop()
                hints.show_hint()
                self.assertTrue(hints.label.isVisible())
                self.assertEqual(hints.label.text(), tips[6])
                QtTest.QTest.qWait(5500)
                self.assertTrue(hints.label.isVisible())
            self.app.sendEvent(header.viewport(), QtCore.QEvent(QtCore.QEvent.Type.Leave))
            self.assertFalse(hints.label.isVisible())
            panel.parameter.setCurrentIndex(0)
            self.assertIn('|Z|', table.horizontalHeaderItem(2).toolTip())
            panel.close()

    def test_main_window_opens_lcr_as_workspace(self):
        import gsmbench.ui as ui
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(ui, 'ROOT', root), patch.object(ui, 'SETTINGS', root / 'settings.json'):
                window = ui.Window()
                window.show_rlc()
                self.assertEqual(window.workspaces.currentIndex(), 3)
                self.assertIsNotNone(window.rlc_window)
                self.assertEqual(window.rlc_window.port.currentText(), 'COM9')
                self.assertIn('rlc', window.settings())
                window.close()


if __name__ == '__main__':
    unittest.main()
