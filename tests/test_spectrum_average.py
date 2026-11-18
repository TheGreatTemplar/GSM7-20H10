import copy
import math
import threading
import unittest
from unittest.mock import Mock

from gsmbench.rsa import AcquisitionCancelled, AnalyzerConfig, average_spectra, demo_spectrum
from gsmbench.rsa_ui import RsaWorker
from tests import test_rsa


class SpectrumAverageTests(unittest.TestCase):
    def test_linear_power_average_preserves_raw_sweeps_and_spread(self):
        first = demo_spectrum(AnalyzerConfig())
        second = demo_spectrum(AnalyzerConfig())
        first["power_dbm"] = [-30] * 801
        second["power_dbm"] = [-20] * 801
        average = average_spectra([first, second], dict(method="spectrum"))
        expected = 10 * math.log10((10 ** -3 + 10 ** -2) / 2)
        self.assertAlmostEqual(average["power_dbm"][0], expected)
        self.assertNotAlmostEqual(average["power_dbm"][0], -25)
        self.assertEqual(average["averaging"]["count"], 2)
        self.assertEqual(average["averaging"]["raw_sweeps"][0]["power_dbm"][0], -30)
        self.assertAlmostEqual(average["averaging"]["marker_std_db"], math.sqrt(50))

    def test_reject_different_settings_and_overload(self):
        first = demo_spectrum(AnalyzerConfig())
        for key, value in (("if_overload", True), ("settings", dict(first["settings"], detector="RMS"))):
            changed = copy.deepcopy(first)
            changed[key] = value
            with self.assertRaises(ValueError):
                average_spectra([first, changed], dict(method="spectrum"))

    def test_worker_uses_fresh_sweeps_and_current_trace_stays_single(self):
        config = AnalyzerConfig()
        worker = RsaWorker("", False, "single", config, {}, 1, 0, None, averages=3)
        driver = Mock()
        driver.acquire.side_effect = [demo_spectrum(config) for _ in range(3)]
        average = worker.capture(driver, config, {}, False)
        self.assertEqual(driver.acquire.call_count, 3)
        self.assertEqual(average["averaging"]["count"], 3)
        worker.operation = "read"
        driver.read_current.return_value = demo_spectrum(config)
        raw = worker.capture(driver, config, {}, False)
        self.assertNotIn("averaging", raw)
        driver.read_current.assert_called_once()

    def test_cancel_between_sweeps_and_source_changes_abort(self):
        config = AnalyzerConfig()
        worker = RsaWorker("", False, "single", config, {}, 1, 0, None, averages=3)
        driver = Mock()
        def acquire(stop):
            stop.set()
            return demo_spectrum(config)
        driver.acquire.side_effect = acquire
        with self.assertRaises(AcquisitionCancelled):
            worker.capture(driver, config, {}, False)
        self.assertEqual(driver.acquire.call_count, 1)
        worker.stop = threading.Event()
        driver.acquire.side_effect = None
        driver.acquire.return_value = demo_spectrum(config)
        source = Mock()
        source.state.return_value = dict(output=0)
        with self.assertRaises(ValueError):
            worker.capture(driver, config, {}, True, source, dict(output=1))

    def test_rms_detector_alias_is_normalized(self):
        driver, device = test_rsa.RsaTests().driver()
        device.spectrum["settings"]["detector"] = "RAV"
        self.assertEqual(driver.metadata()["detector"], "RMS")
        driver.close()
