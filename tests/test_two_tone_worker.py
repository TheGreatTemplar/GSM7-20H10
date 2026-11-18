from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gsmbench.rf_storage import SpectrumStore
from gsmbench.two_tone import TwoTonePlan
from gsmbench.two_tone_ui import TwoToneWorker
from tests.test_smu import make_driver
from tests.test_ssg import make_ssg


class FakeAnalyzer:
    def __init__(self, plan, fail=False):
        self.plan = plan
        self.fail = fail
        self.order = 1

    def configure(self, config):
        self.order = round(config.center_hz / (self.plan.f1_hz + self.plan.spacing_hz / 2))
        return None

    def acquire(self, stop):
        if self.fail:
            raise TimeoutError("test RSA acquisition timeout")
        spectrum = TwoToneWorker(None, {}, []).capture(None, self.plan, self.order)
        spectrum["demo"] = False
        spectrum["identity"] = "FAKE analyzer used only with a temporary test database"
        return spectrum

    def close(self):
        pass


class WorkerTests(unittest.TestCase):
    def test_initial_connection_failure_does_not_lock_ui_as_if_rf_was_enabled(self):
        worker = TwoToneWorker(None, dict(demo=False, akip_host="127.0.0.1", akip_port=5025), [])
        states = []
        worker.outputs.connect(states.append)
        with patch("gsmbench.two_tone_ui.Ssg3000x", side_effect=TimeoutError("offline")):
            worker.run()
        self.assertEqual(states[-1], dict(known=False, active=False, requires_off=False))

    def run_worker(self, directory, plans, fail=False, stop=False, bad_level=False):
        smu, smu_device = make_driver()
        akip, akip_device = make_ssg()
        akip_device.bad_level = bad_level
        settings = dict(demo=False, root=directory, database=str(Path(directory) / "mixers.db"),
            akip_host="127.0.0.1", akip_port=5025, smu_host="127.0.0.1", smu_port=5025,
            rsa_host="127.0.0.1", rf_channel=2, lo_channel=1, sample="PA-test", batch="", model="", notes="", settling_s=0)
        worker = TwoToneWorker(None, settings, plans)
        errors, states, points = [], [], []
        worker.failed.connect(errors.append)
        worker.outputs.connect(states.append)
        worker.point_ready.connect(points.append)
        if stop:
            worker.point_ready.connect(lambda point: worker.stop.set())
        with patch("gsmbench.two_tone_ui.Smu200a", return_value=smu), \
                patch("gsmbench.two_tone_ui.Ssg3000x", return_value=akip), \
                patch("gsmbench.two_tone_ui.Rsa6265", return_value=FakeAnalyzer(plans[0], fail)):
            worker.run()
        self.assertEqual(states[-1], dict(known=True, active=False))
        self.assertTrue(all(not value["output"] for value in smu_device.states.values()))
        self.assertEqual(akip_device.values[":OUTP?"], "0")
        return errors, points, smu_device, akip_device, settings

    def test_series_enables_each_tone_once_and_saves_harmonics(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = replace(TwoTonePlan(), harmonics=3)
            errors, points, smu, akip, settings = self.run_worker(directory, [plan, replace(plan, level1_dbm=-19, level2_dbm=-19)])
            self.assertEqual(errors, [])
            self.assertEqual(len(points), 2)
            self.assertEqual(smu.commands.count(":OUTP2:STAT ON"), 1)
            self.assertEqual(smu.commands.count(":OUTP1:STAT ON"), 1)
            self.assertNotIn(":OUTP ON", akip.commands)
            self.assertFalse(any(command.startswith((":FREQ ", ":POW ")) for command in akip.commands))
            rows = SpectrumStore(settings["database"], False).load()
            self.assertEqual(len(rows), 6)
            for row in rows:
                channels = row["generator"]["channels"]
                self.assertTrue(channels["1"]["output"] and channels["2"]["output"])
                self.assertEqual(channels["1"]["frequency_hz"], plan.f1_hz)
                self.assertEqual(channels["2"]["frequency_hz"], plan.f2_hz)
                self.assertEqual(channels["1"]["level_dbm"], channels["2"]["level_dbm"])
                self.assertFalse(row["generator2"]["output"])

    def test_mixer_uses_both_smu_tones_and_akip_lo(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = replace(TwoTonePlan(), mode="mixer", output_loss_db=0, harmonics=1,
                level1_dbm=-25, level2_dbm=-24.7, lo_hz=25059200, lo_dbm=17)
            errors, points, smu, akip, settings = self.run_worker(directory, [plan])
            self.assertEqual(errors, [])
            self.assertEqual(len(points), 1)
            self.assertEqual(smu.commands.count(":OUTP1:STAT ON"), 1)
            self.assertEqual(smu.commands.count(":OUTP2:STAT ON"), 1)
            self.assertEqual(akip.commands.count(":OUTP ON"), 1)
            row = SpectrumStore(settings["database"], False).load()[0]
            channels = row["generator"]["channels"]
            self.assertEqual(channels["1"]["frequency_hz"], plan.f1_hz)
            self.assertEqual(channels["2"]["frequency_hz"], plan.f2_hz)
            self.assertEqual(channels["1"]["level_dbm"], plan.level1_dbm)
            self.assertEqual(channels["2"]["level_dbm"], plan.level2_dbm)
            self.assertEqual(row["generator2"]["frequency_hz"], plan.lo_hz)
            self.assertEqual(row["generator2"]["level_dbm"], plan.lo_dbm)
            self.assertTrue(row["generator2"]["output"])
            self.assertEqual(row["context"]["source_layout"], "smu_ab_akip_lo")

    def test_timeout_and_stop_always_disable_both_generators(self):
        plan = replace(TwoTonePlan(), harmonics=1)
        with tempfile.TemporaryDirectory() as directory:
            errors, _, _, _, _ = self.run_worker(directory, [plan], fail=True)
            self.assertTrue(any("timeout" in error for error in errors))
        with tempfile.TemporaryDirectory() as directory:
            errors, points, _, _, _ = self.run_worker(directory, [plan, plan], stop=True)
            self.assertEqual(errors, [])
            self.assertEqual(len(points), 1)

    def test_bad_akip_readback_never_enables_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            errors, points, smu, akip, _ = self.run_worker(directory,
                [replace(TwoTonePlan(), mode="mixer", output_loss_db=0, harmonics=1)], bad_level=True)
            self.assertTrue(errors)
            self.assertFalse(points)
            self.assertNotIn(":OUTP2:STAT ON", smu.commands)
            self.assertNotIn(":OUTP1:STAT ON", smu.commands)
            self.assertNotIn(":OUTP ON", akip.commands)
