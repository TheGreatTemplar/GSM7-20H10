from dataclasses import asdict, replace
import math
from pathlib import Path
import tempfile
import unittest

from PySide6 import QtWidgets
from PySide6 import QtGui

from gsmbench.rf_storage import SpectrumStore
from gsmbench.rsa_ui import RsaWindow
from gsmbench.two_tone import TwoTonePlan, analyze_two_tone
from gsmbench.two_tone_ui import TwoToneWorker
from gsmbench.two_tone_results import columns, row_values


class ResultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def point(self, plan):
        worker = TwoToneWorker(None, {}, [])
        spectrum = worker.capture(None, plan, 1)
        spectrum['context'] = dict(method='two_tone_pa', two_tone_plan=asdict(plan))
        result = analyze_two_tone(spectrum)
        self.assertEqual(result['status'], 'preliminary', result)
        return dict(plan=asdict(plan), result=result, spectrum_id=spectrum['id'], harmonics=[]), spectrum

    def test_nominal_ip3_estimate_uses_each_side_and_stays_labelled(self):
        point, _ = self.point(TwoTonePlan())
        for side in ('low', 'high'):
            result = point['result']
            item = result['sides'][side]
            self.assertIsNone(item['ip3'])
            self.assertAlmostEqual(item['ip3_estimate']['oip3_dbm'],
                result['fundamental_' + side + '_dbm'] - item['im3_dbc'] / 2)
        values = row_values(point)
        self.assertEqual(len(values), len(columns()))
        self.assertIsNotNone(values[16])
        self.assertIn('Номинальные', values[25])

    def test_noise_limited_point_has_no_ip3_but_has_limit(self):
        point, _ = self.point(replace(TwoTonePlan(), level1_dbm=-60, level2_dbm=-60))
        values = row_values(point)
        self.assertTrue(values[12].startswith('<'))
        self.assertIsNone(values[16])

    def test_averages_persist_raw_sweeps_and_stop_cancels(self):
        plan = replace(TwoTonePlan(), sweep_averages=5, detector='RMS')
        point, spectrum = self.point(plan)
        self.assertEqual(spectrum['averaging']['count'], 5)
        self.assertEqual(len(spectrum['averaging']['raw_sweeps']), 5)
        worker = TwoToneWorker(None, {}, [])
        worker.stop.set()
        from gsmbench.rsa import AcquisitionCancelled
        with self.assertRaises(AcquisitionCancelled):
            worker.capture(None, plan, 1)

    def test_small_driver_preset_plans_and_all_plot_choices(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = QtWidgets.QWidget()
            bench = RsaWindow(owner, Path(directory), dict(demo=True, database=str(Path(directory)/'demo.db')))
            panel = bench.two_tone
            # QWidget.metric is a virtual Qt method used when the styled parent
            # lays out/paints this tab. A selector named 'metric' shadows it.
            self.assertTrue(callable(panel.results.metric))
            self.assertGreater(panel.results.metric(QtGui.QPaintDevice.PaintDeviceMetric.PdmDpiX), 0)
            panel.driver_preset()
            plans = panel.plans()
            self.assertEqual(len(plans), 33)
            self.assertEqual(plans[0].level1_dbm - plans[0].loss1_db, -10)
            self.assertEqual(plans[-1].level1_dbm - plans[-1].loss1_db, 0)
            self.assertEqual(plans[0].reference_dbm, 0)
            self.assertEqual(plans[0].sweep_averages, 5)
            self.assertEqual(plans[0].detector, 'RMS')
            # Synthetic points with resolved IM3 to exercise every graph and axis.
            panel.points = [self.point(replace(plan, sweep_averages=1,
                level1_dbm=plan.level1_dbm-10, level2_dbm=plan.level2_dbm-10))[0] for plan in plans[::10]]
            panel.results.update_points()
            self.assertEqual(panel.table.rowCount(), len(panel.points))
            for metric in range(panel.results.metric_selector.count()):
                panel.results.metric_selector.setCurrentIndex(metric)
                for axis in range(panel.results.x_axis.count()):
                    panel.results.x_axis.setCurrentIndex(axis)
                    self.assertTrue(panel.plot.listDataItems())
            panel.results.frequency.setCurrentIndex(1)
            self.assertEqual(panel.table.rowCount(), sum(point['plan']['f1_hz'] == panel.results.frequency.currentData() for point in panel.points))
            panel.results.frequency.setCurrentIndex(0)
            panel.fields['drive_step'].setValue(10)
            self.assertEqual(len(panel.plans()), 6)
            for thread in bench.findChildren(__import__('PySide6').QtCore.QThread):
                thread.wait()
            self.app.processEvents()
            owner.close()

    def test_legacy_plan_loads_without_new_fields(self):
        plan = asdict(TwoTonePlan())
        del plan['sweep_averages']
        del plan['detector']
        self.assertEqual(TwoTonePlan(**plan).sweep_averages, 1)

    def test_upper_lo_gain_tracks_corresponding_input_tone(self):
        point, _ = self.point(replace(TwoTonePlan(), mode='mixer', output_loss_db=0,
            harmonics=1, level2_dbm=-19.8))
        values = row_values(point)
        self.assertAlmostEqual(values[8], point['result']['fundamental_low_dbm'] - values[3])
        self.assertAlmostEqual(values[9], point['result']['fundamental_high_dbm'] - values[2])

    def test_multiline_frequency_series_preview_and_single_frequency_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = QtWidgets.QWidget()
            bench = RsaWindow(owner, Path(directory), dict(demo=True, database=str(Path(directory)/'demo.db')))
            panel = bench.two_tone
            panel.analyzer_preset()
            panel.fields['f1_mhz'].setValue(7.1)
            editor = panel.fields['frequency_ranges']
            editor.setPlainText('14; 14.175; 14.35\n50..52/1')
            self.assertFalse(panel.fields['f1_mhz'].isEnabled())
            self.assertEqual(panel.fields['f1_mhz'].value(), 14)
            self.assertEqual(panel.f2_mhz.text(), '14,002000')
            self.assertEqual(len(panel.plans()), 6)
            saved = panel.settings()
            panel.restore(saved)
            self.assertEqual(panel.fields['f1_mhz'].value(), 14)
            self.assertEqual(panel.settings()['single_frequency_mhz'], 7.1)
            editor.clear()
            self.assertTrue(panel.fields['f1_mhz'].isEnabled())
            self.assertEqual(panel.fields['f1_mhz'].value(), 7.1)
            self.assertEqual(panel.f2_mhz.text(), '7,102000')
            editor.setPlainText('50..52/ошибка')
            self.assertEqual(panel.f2_mhz.text(), '—')
            for thread in bench.findChildren(__import__('PySide6').QtCore.QThread):
                thread.wait()
            self.app.processEvents()
            owner.close()
