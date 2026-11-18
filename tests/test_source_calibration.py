import copy
from dataclasses import asdict, replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gsmbench.rsa import demo_spectrum, frequencies
from gsmbench.source_calibration import CalibrationPlan, PowerCalibrationWorker, calibrated_source_plan, load_profiles
from gsmbench.nrp18t import PowerReadingError
from gsmbench.two_tone import TwoTonePlan
from gsmbench.two_tone_ui import TwoToneWorker
from tests.test_smu import make_driver


class FakeCalibrationAnalyzer:
    identity = "FAKE,RSA6265"

    def __init__(self, device, fail=False):
        self.device, self.fail = device, fail

    def configure(self, config):
        self.config = config
        return demo_spectrum(config)["settings"]

    def acquire(self, stop):
        if self.fail:
            raise TimeoutError("test acquisition failure")
        spectrum = demo_spectrum(self.config)
        states = self.device.states
        assert all(state["output"] for state in states.values())
        targets = [(state["frequency_hz"], state["level_dbm"] - 6 + (0.23 if channel == 1 else -0.17))
            for channel, state in states.items()]
        spectrum["power_dbm"] = [max([-110] + [level - 3 * ((hz - target) / (self.config.rbw_hz / 2)) ** 2
            for target, level in targets]) for hz in frequencies(spectrum)]
        return spectrum

    def close(self):
        pass


class FakeCalibrationSensor:
    identity = "FAKE,NRP18T"

    def __init__(self, device):
        self.device = device
        self.channels = []
        self.reject_first = False
        self.timeout_once = False
        self.unstable_first = False
        self.oversized_first = False

    def prepare(self, stop, averages=16):
        assert not any(state["output"] for state in self.device.states.values())
        self.averages = averages
        return dict(averages=averages)

    def start_level(self, frequency, stop):
        active = [state for state in self.device.states.values() if state["output"]]
        assert len(active) == 1 and active[0]["frequency_hz"] == frequency
        return dict(discarded_dbm=-30)

    def read_dbm(self, frequency, stop):
        active = [channel for channel, state in self.device.states.items() if state["output"]]
        assert len(active) == 1, "Power sensor must see one tone only"
        channel = active[0]
        self.channels.append(channel)
        state = self.device.states[channel]
        assert frequency == state["frequency_hz"]
        if self.reject_first and state["level_dbm"] < -19.5:
            raise PowerReadingError("test sensor below its measurement range")
        if self.timeout_once:
            self.timeout_once = False
            raise TimeoutError("test sensor timeout")
        if self.unstable_first and len(self.channels) <= 24:
            sign = 1 if ((len(self.channels) - 1) // 4) % 2 == 0 else -1
            return state["level_dbm"] - 6 + sign * 0.3
        if self.oversized_first and len(self.channels) <= 4:
            return state["level_dbm"] - 6 + 4
        return state["level_dbm"] - 6 + (0.23 if channel == 1 else -0.17)

    def close(self):
        pass


class CalibrationTests(unittest.TestCase):
    def run_profile(self, directory, method="rsa", fail=False, cancel=False, reject_first=False, timeout_once=False,
            unstable_first=False, oversized_first=False, plan_override=None):
        driver, device = make_driver()
        sensor = FakeCalibrationSensor(device)
        sensor.reject_first = reject_first
        sensor.timeout_once = timeout_once
        sensor.unstable_first = unstable_first
        sensor.oversized_first = oversized_first
        settings = dict(root=directory, demo=False, smu_host="127.0.0.1", smu_port=5025,
            rsa_host="127.0.0.1", nrp_resource="USB-test", calibration_path="path-A", settling_s=0,
            nrp_settling_s=0, rsa_attenuation=20, rsa_reference=10)
        plan = plan_override or CalibrationPlan(14e6, 14.002e6, 6, 6, first_dbm=-26, last_dbm=-24, samples=2, method=method)
        worker = PowerCalibrationWorker(None, settings, plan)
        updates, errors, outputs = [], [], []
        worker.profile_ready.connect(updates.append)
        worker.failed.connect(errors.append)
        worker.outputs.connect(outputs.append)
        if cancel:
            worker.profile_ready.connect(lambda profile: worker.stop.set())
        with patch("gsmbench.source_calibration.Smu200a", return_value=driver), \
                patch("gsmbench.source_calibration.Rsa6265", return_value=FakeCalibrationAnalyzer(device, fail)), \
                patch("gsmbench.source_calibration.Nrp18t", return_value=sensor):
            worker.run()
        self.assertFalse(any(state["output"] for state in device.states.values()))
        self.assertEqual(outputs[-1], dict(known=True, active=False, requires_off=False))
        return updates[-1], errors, settings, sensor

    def test_rsa_converges_and_corrections_are_applied_to_real_source_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, errors, settings, _ = self.run_profile(directory)
            self.assertEqual(errors, [])
            self.assertEqual(profile["state"], "complete")
            self.assertEqual(len(profile["points"]), 3)
            point = profile["points"][0]
            self.assertAlmostEqual(point["commands_dbm"][0], -20.23)
            self.assertAlmostEqual(point["commands_dbm"][1], -19.83)
            self.assertAlmostEqual(point["measured_dbm"][0], -26)
            self.assertAlmostEqual(point["measured_dbm"][1], -26)
            self.assertTrue(profile["attempts"][0]["raw"])
            settings["power_calibration"] = profile
            worker = TwoToneWorker(None, settings, [])
            source = worker.source_plan(TwoTonePlan())
            self.assertAlmostEqual(source.rf_dbm, -20.23)
            self.assertAlmostEqual(source.lo_dbm, -19.83)
            self.assertEqual(len(load_profiles(directory, False)), 1)
            self.assertEqual(load_profiles(directory, True), [])

    def test_nrp_zeros_with_outputs_off_and_never_measures_two_channels_together(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, errors, _, sensor = self.run_profile(directory, "nrp")
            self.assertEqual(errors, [])
            self.assertEqual(profile["state"], "complete")
            self.assertEqual(set(sensor.channels), {1, 2})
            self.assertEqual(sensor.averages, 128)

    def test_failed_and_cancelled_profiles_are_saved_but_cannot_be_applied(self):
        for fail, cancel in ((True, False), (False, True)):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                profile, _, settings, _ = self.run_profile(directory, fail=fail, cancel=cancel)
                self.assertEqual(profile["state"], "failed" if fail else "cancelled")
                self.assertEqual(load_profiles(directory, False), [])
                if profile["points"]:
                    self.assertEqual(len(load_profiles(directory, False, include_incomplete=True)), 1)
                self.assertEqual(len(list((Path(directory) / "data/smu-calibrations").glob("*.json"))), 1)
                with self.assertRaises(ValueError):
                    calibrated_source_plan(profile, TwoTonePlan(), settings)

    def test_mismatched_paths_frequencies_levels_and_modes_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, _, settings, _ = self.run_profile(directory)
            for plan in (replace(TwoTonePlan(), f1_hz=7e6), replace(TwoTonePlan(), loss1_db=7),
                    replace(TwoTonePlan(), level1_dbm=-19.5, level2_dbm=-19.5)):
                with self.assertRaises(ValueError):
                    calibrated_source_plan(profile, plan, settings)
            for change in (dict(calibration_path="other"), dict(demo=True), dict(smu_host="other")):
                with self.assertRaises(ValueError):
                    calibrated_source_plan(profile, TwoTonePlan(), dict(settings, **change))
            damaged = copy.deepcopy(profile)
            damaged["points"].pop()
            with self.assertRaises(ValueError):
                calibrated_source_plan(damaged, TwoTonePlan(), settings)

    def test_nrp_range_and_grid_limits(self):
        plan = CalibrationPlan(14e6, 14.002e6, 6, 6)
        for bad in (replace(plan, method="nrp", first_dbm=-40), replace(plan, step_db=0),
                replace(plan, last_dbm=21), replace(plan, tolerance_db=0), replace(plan, f2_hz=14e6)):
            with self.assertRaises(ValueError):
                bad.levels()

    def test_sensor_failure_skips_level_and_only_successful_levels_can_be_applied(self):
        for option in (dict(reject_first=True), dict(timeout_once=True)):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as directory:
                profile, errors, settings, _ = self.run_profile(directory, method="nrp", **option)
                self.assertEqual(errors, [])
                self.assertEqual(profile["state"], "complete_with_gaps")
                self.assertEqual([point["target_dbm"] for point in profile["skipped"]], [-26])
                self.assertEqual([point["target_dbm"] for point in profile["points"]], [-25, -24])
                self.assertEqual(len(load_profiles(directory, False)), 1)
                source = calibrated_source_plan(profile, replace(TwoTonePlan(), level1_dbm=-19, level2_dbm=-19), settings)
                self.assertAlmostEqual(source.rf_dbm, -19.23)
                with self.assertRaisesRegex(ValueError, "Нет калибровки"):
                    calibrated_source_plan(profile, TwoTonePlan(), settings)

    def test_frequency_grid_and_sweep_create_a_profile_for_every_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = CalibrationPlan(14e6, 14.002e6, 6, 6, first_dbm=-26, last_dbm=-24, samples=1,
                stop_frequency_hz=16.5e6, frequency_step_hz=1e6)
            self.assertEqual([pair[0] for pair in plan.frequency_pairs()], [14e6, 15e6, 16e6, 16.5e6])
            settings = dict(root=directory, demo=True, smu_host="test", smu_port=5025, rsa_host="test",
                nrp_resource="USB-test", calibration_path="test", settling_s=0, rsa_attenuation=20, rsa_reference=10)
            worker = PowerCalibrationWorker(None, settings, plan)
            with patch("gsmbench.source_calibration.Smu200a", side_effect=AssertionError("Demo RF")):
                worker.run()
            self.assertEqual(worker.result_state, "complete")
            profiles = load_profiles(directory, True)
            self.assertEqual(len(profiles), 4)
            self.assertEqual(len({profile["calibration_batch_id"] for profile in profiles}), 1)
            self.assertTrue(all(len(profile["points"]) == 3 for profile in profiles))
            self.assertTrue(all(profile["rf_off_confirmed"] for profile in profiles))

    def test_nonconvergence_and_excessive_correction_skip_level_and_continue(self):
        for option in (dict(unstable_first=True), dict(oversized_first=True)):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as directory:
                profile, errors, settings, _ = self.run_profile(directory, method="nrp", **option)
                self.assertEqual(errors, [])
                self.assertEqual(profile["state"], "complete_with_gaps")
                self.assertEqual([point["target_dbm"] for point in profile["skipped"]], [-26])
                self.assertEqual([point["target_dbm"] for point in profile["points"]], [-25, -24])
                self.assertEqual(profile["skipped"][0]["iteration"], 6 if option.get("unstable_first") else 1)
                self.assertEqual(len(load_profiles(directory, False)), 1)
                with self.assertRaisesRegex(ValueError, "Нет калибровки"):
                    calibrated_source_plan(profile, TwoTonePlan(), settings)

    def test_unreachable_corrected_power_is_skipped_without_commanding_above_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = CalibrationPlan(14e6, 14.002e6, 6, 6, first_dbm=13, last_dbm=14, samples=2, method="nrp")
            profile, errors, _, sensor = self.run_profile(directory, method="nrp", plan_override=plan)
            self.assertEqual(errors, [])
            self.assertEqual([point["target_dbm"] for point in profile["points"]], [13])
            self.assertEqual([point["target_dbm"] for point in profile["skipped"]], [14])
            powers = [float(command.rsplit(" ", 1)[1]) for command in sensor.device.commands
                if ":POW:LEV:IMM:AMPL " in command]
            self.assertLessEqual(max(powers), 20)

    def test_vhf_first_tone_can_reach_200_mhz(self):
        plan = CalibrationPlan(144e6, 144.002e6, 6, 6, stop_frequency_hz=200e6, frequency_step_hz=28e6)
        self.assertEqual(plan.frequency_pairs(), [(144e6, 144.002e6), (172e6, 172.002e6), (200e6, 200.002e6)])
        replace(TwoTonePlan(), f1_hz=200e6).validate()

    def test_nominal_levels_outside_hardware_limits_are_skipped_after_valid_levels(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = CalibrationPlan(14e6, 14.002e6, 6, 6, first_dbm=13, last_dbm=15, samples=2, method="nrp")
            profile, errors, _, _ = self.run_profile(directory, method="nrp", plan_override=plan)
            self.assertEqual(errors, [])
            self.assertEqual(profile["state"], "complete_with_gaps")
            self.assertEqual([point["target_dbm"] for point in profile["points"]], [13])
            self.assertEqual([point["target_dbm"] for point in profile["skipped"]], [14, 15])

    def test_nrp_common_frequency_profiles_supply_each_tone_at_its_own_frequency(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = CalibrationPlan(14e6, 14e6, 6, 6, first_dbm=-26, last_dbm=-24, samples=1,
                method="nrp", stop_frequency_hz=14.002e6, frequency_step_hz=2000)
            settings = dict(root=directory, demo=True, smu_host="test", smu_port=5025, rsa_host="test",
                nrp_resource="USB-test", calibration_path="test", settling_s=0, nrp_settling_s=0)
            worker = PowerCalibrationWorker(None, settings, plan)
            worker.run()
            profiles = load_profiles(directory, True)
            self.assertEqual(len(profiles), 2)
            self.assertTrue(all(item["plan"]["f1_hz"] == item["plan"]["f2_hz"] for item in profiles))
            settings["power_calibration_profiles"] = profiles
            source = calibrated_source_plan(profiles[0], TwoTonePlan(), settings)
            self.assertAlmostEqual(source.rf_dbm, -20.23)
            self.assertAlmostEqual(source.lo_dbm, -19.83)
            with self.assertRaisesRegex(ValueError, "нет калибровки канала B"):
                calibrated_source_plan(profiles[0], replace(TwoTonePlan(), spacing_hz=3000), settings)
