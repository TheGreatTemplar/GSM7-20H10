import copy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import Mock

from gsmbench.detector import DetectorPlan, DetectorStore, voltage_to_dbm
from gsmbench.detector_ui import DetectorWorker
from tests.test_smu import make_driver
from tests.test_ssg import make_ssg


class FakeMeter:
    identity = "FAKE,34401A"

    def __init__(self, fail=False):
        self.count = 0
        self.fail = fail
        self.closed = False

    def configure_dc(self, *args):
        return dict(identity=self.identity)

    def read_voltage(self, stop):
        self.count += 1
        if self.fail and self.count > 3:
            raise TimeoutError("test meter timeout")
        return self.count / 1000

    def close(self):
        self.closed = True


class DetectorTests(unittest.TestCase):
    def test_offline_source_before_rf_enable_does_not_require_off_confirmation(self):
        settings = dict(demo=False, source="akip", akip_host="127.0.0.1", akip_port=5025)
        worker = DetectorWorker(None, settings, DetectorPlan(), "series")
        states = []
        worker.outputs.connect(states.append)
        with patch("gsmbench.detector.Ssg3000x", side_effect=TimeoutError("could not connect: -1073807339")):
            worker.run()
        self.assertEqual(states[-1], dict(known=False, active=False, requires_off=False))

    def test_failed_enable_then_failed_off_still_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = dict(root=directory, database=str(Path(directory) / "detectors.db"), demo=False,
                source="akip", sample="DET001", notes="", akip_host="127.0.0.1", akip_port=5025,
                smu_host="127.0.0.1", smu_port=5025, com_port="COM3", baud=9600)
            source = Mock()
            source.setpoint.return_value = dict(output=0)
            source.enable.side_effect = TimeoutError("reply lost after ON")
            source.off.side_effect = TimeoutError("OFF not confirmed")
            worker = DetectorWorker(None, settings, replace(DetectorPlan(), settling_s=0), "series")
            states = []
            worker.outputs.connect(states.append)
            with patch.dict("gsmbench.detector_ui.SOURCE_FACTORIES", {"akip": lambda *args: source}), \
                    patch("gsmbench.detector_ui.Hp34401a", return_value=FakeMeter()):
                worker.run()
            self.assertEqual(states[-1], dict(known=False, active=True, requires_off=True))
            source.off.assert_called_once()

    def test_levels_endpoint_and_limits(self):
        plan = replace(DetectorPlan(), start_dbm=-3, stop_dbm=0, step_db=2)
        self.assertEqual(plan.levels(), [-3, -1, 0])
        self.assertEqual(plan.levels(True), [-3])
        for bad in (replace(plan, step_db=0), replace(plan, stop_dbm=21), replace(plan, samples=0), replace(plan, step_db=0.001)):
            with self.assertRaises(ValueError):
                bad.levels()

    def test_monotonic_interpolation_and_no_extrapolation(self):
        points = [dict(input_dbm=-10, voltage_v=0.1), dict(input_dbm=0, voltage_v=0.3)]
        self.assertAlmostEqual(voltage_to_dbm(points, 0.2), -5)
        with self.assertRaises(ValueError):
            voltage_to_dbm(points, 0.4)
        points.append(dict(input_dbm=10, voltage_v=0.2))
        with self.assertRaises(ValueError):
            voltage_to_dbm(points, 0.2)

    def run_worker(self, directory, source_kind="akip", fail=False, stop=False):
        akip, akip_device = make_ssg()
        smu, smu_device = make_driver()
        meter = FakeMeter(fail)
        settings = dict(root=directory, database=str(Path(directory) / "detectors.db"), demo=False,
            source=source_kind, sample="DET001", notes="", akip_host="127.0.0.1", akip_port=5025,
            smu_host="127.0.0.1", smu_port=5025, com_port="COM3", baud=9600)
        plan = replace(DetectorPlan(), start_dbm=-30, stop_dbm=-28, settling_s=0)
        worker = DetectorWorker(None, settings, plan, "series")
        updates, errors, outputs = [], [], []
        worker.updated.connect(updates.append)
        worker.failed.connect(errors.append)
        worker.outputs.connect(outputs.append)
        if stop:
            worker.updated.connect(lambda run: worker.stop.set())
        with patch("gsmbench.detector.Ssg3000x", return_value=akip), \
                patch("gsmbench.detector.Smu200a", return_value=smu), \
                patch("gsmbench.detector_ui.Hp34401a", return_value=meter):
            worker.run()
        self.assertEqual(outputs[-1], dict(known=True, active=False))
        self.assertEqual(akip_device.values[":OUTP?"], "0")
        self.assertTrue(all(not state["output"] for state in smu_device.states.values()))
        self.assertTrue(meter.closed)
        return updates, errors, akip_device, smu_device, settings

    def test_akip_series_saved_and_output_enabled_once(self):
        with tempfile.TemporaryDirectory() as directory:
            updates, errors, device, _, settings = self.run_worker(directory)
            self.assertEqual(errors, [])
            self.assertEqual(len(updates[-1]["points"]), 3)
            self.assertEqual(device.commands.count(":OUTP ON"), 1)
            store = DetectorStore(settings["database"], False)
            self.assertEqual(store.load()[0]["state"], "complete")
            self.assertFalse(list((Path(directory) / "data" / "detector-pending").glob("*.json")))
            imported = DetectorStore(Path(directory) / "import.db", False)
            imported.save_many(store.load())
            changed = copy.deepcopy(store.load()[0])
            changed["notes"] = "conflict"
            with self.assertRaises(ValueError):
                imported.save_many([changed])

    def test_either_smu_channel_and_unused_channel_off(self):
        for kind, channel in (("smu_a", 1), ("smu_b", 2)):
            with tempfile.TemporaryDirectory() as directory:
                updates, errors, _, device, _ = self.run_worker(directory, kind)
                self.assertEqual(errors, [])
                self.assertEqual(device.commands.count(f":OUTP{channel}:STAT ON"), 1)
                self.assertNotIn(f":OUTP{3 - channel}:STAT ON", device.commands)
                self.assertEqual(updates[-1]["points"][0]["source"]["channel"], channel)

    def test_timeout_and_cancel_keep_partial_data_and_off(self):
        for fail, stop, state in ((True, False, "failed"), (False, True, "cancelled")):
            with tempfile.TemporaryDirectory() as directory:
                updates, errors, _, _, settings = self.run_worker(directory, fail=fail, stop=stop)
                self.assertEqual(DetectorStore(settings["database"], False).load()[0]["state"], state)
                self.assertEqual(bool(errors), fail)
                self.assertEqual(len(updates[-1]["points"]), 0 if fail else 1)
