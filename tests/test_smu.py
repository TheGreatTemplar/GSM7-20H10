import copy
import threading
import unittest
from unittest.mock import Mock, patch

from gsmbench.rf_sequence import active_channels, matrix_axes, plan_sequence, sequence_count
from gsmbench.rf_methods import ISOLATION_PORTS, target_frequency
from gsmbench.rsa import AcquisitionCancelled, AnalyzerConfig, demo_spectrum
from gsmbench.rsa_ui import RsaWorker
from gsmbench.smu import Smu200a, SourcePlan, verified_off


SETTINGS = dict(host="127.0.0.1", port=5025, rf_channel=2, lo_channel=1, rf_dbm=-20,
                lo_dbm=13, rf_loss_db=1, lo_loss_db=2, source_path_known=True,
                settling_s=0, sweep_kind="rf_frequency", sweep_start=14, sweep_stop=15)
CONTEXT = dict(rf_hz=14e6, lo_hz=25059200, if_hz=11059200, method="conversion",
               sample="MX001", notes="", output_loss_db=0, path_known=True, marker_window_hz=3000)


class FakeSmuVisa:
    def __init__(self):
        self.commands = []
        self.closed = False
        self.states = {channel: dict(output=0, frequency_hz=1e6, level_dbm=-30,
            frequency_mode="CW", power_mode="CW", modulation=0, multiplier=1,
            frequency_offset_hz=0, power_offset_db=0, correction=0, alc="AUTO", power_limit_dbm=20)
            for channel in (1, 2)}
        self.bad_frequency = False
        self.throw_query = None

    def query(self, command):
        self.commands.append(command)
        if ";" in command:
            return ";".join(self.query(part) for part in command.split(";"))
        if command == self.throw_query:
            raise TimeoutError("test SMU timeout")
        if command == "*IDN?":
            return "Rohde&Schwarz,SMU200A,123,3.1"
        if command == "*OPT?":
            return "SMU-B103, SMU-B203"
        if command == ":SYST:ERR?":
            return '0,"No error"'
        channel = int(command[5] if command.startswith(":SOUR") else command[5])
        state = self.states[channel]
        if command.startswith(":OUTP"):
            return str(state["output"])
        suffix = command.split(":", 2)[2]
        if suffix == "FREQ:CW? MIN":
            return "100000"
        if suffix == "FREQ:CW? MAX":
            return "3000000000"
        if suffix == "POW? MIN":
            return "-145"
        if suffix == "POW? MAX":
            return "20"
        keys = {"FREQ:CW?": "frequency_hz", "FREQ:MODE?": "frequency_mode",
                "POW:LEV:IMM:AMPL?": "level_dbm", "POW:MODE?": "power_mode",
                "MOD:ALL:STAT?": "modulation", "FREQ:MULT?": "multiplier",
                "FREQ:OFFS?": "frequency_offset_hz", "POW:OFFS?": "power_offset_db",
                "CORR:STAT?": "correction", "POW:LIM?": "power_limit_dbm", "POW:ALC:STAT?": "alc"}
        value = state[keys[suffix]]
        return str(value + 1 if self.bad_frequency and suffix == "FREQ:CW?" else value)

    def write(self, command):
        self.commands.append(command)
        if command.startswith(":OUTP"):
            channel = int(command[5])
            self.states[channel]["output"] = int(command.endswith(" ON"))
            return
        channel = int(command[5])
        suffix, value = command.split(":", 2)[2].rsplit(" ", 1)
        keys = {"FREQ:CW": ("frequency_hz", float), "FREQ:MODE": ("frequency_mode", str),
                "POW:LEV:IMM:AMPL": ("level_dbm", float), "POW:MODE": ("power_mode", str),
                "MOD:ALL:STAT": ("modulation", lambda value: int(value == "ON")),
                "FREQ:MULT": ("multiplier", float), "FREQ:OFFS": ("frequency_offset_hz", float),
                "POW:OFFS": ("power_offset_db", float), "CORR:STAT": ("correction", lambda value: int(value == "ON")),
                "POW:ALC:STAT": ("alc", str)}
        key, cast = keys[suffix]
        self.states[channel][key] = cast(value)

    def close(self):
        self.closed = True


def make_driver(device=None):
    device = device or FakeSmuVisa()
    manager = Mock()
    manager.open_resource.return_value = device
    return Smu200a("127.0.0.1", manager_factory=Mock(return_value=manager)), device


class SmuTests(unittest.TestCase):
    def test_cw_setup_readback_and_output_order(self):
        driver, device = make_driver()
        plan = SourcePlan(14e6, 25059200)
        driver.prepare(plan)
        self.assertTrue(all(not state["output"] for state in device.states.values()))
        self.assertFalse(any(command.endswith("STAT ON") and command.startswith(":OUTP") for command in device.commands))
        driver.enable((1, 2), threading.Event())
        self.assertTrue(all(state["output"] for state in device.states.values()))
        verified_off(driver)
        self.assertTrue(all(not state["output"] for state in device.states.values()))
        driver.close()
        self.assertTrue(device.closed)

    def test_invalid_mapping_limits_and_bad_readback_never_enable_rf(self):
        driver, device = make_driver()
        for plan in (SourcePlan(14e6, 25e6, rf_channel=1), SourcePlan(14e6, 25e6, lo_dbm=21)):
            with self.assertRaises(ValueError):
                driver.prepare(plan)
        device.states[1]["power_limit_dbm"] = 10
        with self.assertRaises(ValueError):
            driver.prepare(SourcePlan(14e6, 25e6))
        device.states[1]["power_limit_dbm"] = 20
        device.bad_frequency = True
        with self.assertRaises(ValueError):
            driver.prepare(SourcePlan(14e6, 25e6))
        self.assertFalse(any(command.startswith(":OUTP") and command.endswith(" ON") for command in device.commands))
        driver.close()

    def test_timeout_cannot_confirm_off_from_stale_reply_and_tries_both_writes(self):
        driver, device = make_driver()
        device.throw_query = ":OUTP1:STAT?"
        with self.assertRaises(TimeoutError):
            driver.query(":OUTP1:STAT?")
        with self.assertRaises(RuntimeError):
            driver.all_off()
        self.assertIn(":OUTP1:STAT OFF", device.commands)
        self.assertIn(":OUTP2:STAT OFF", device.commands)
        fresh = Mock()
        fresh.state.return_value = dict(confirmed=True)
        with patch("gsmbench.smu.Smu200a", return_value=fresh):
            self.assertEqual(verified_off(driver), dict(confirmed=True))
        fresh.all_off.assert_called_once()
        fresh.close.assert_called_once()
        driver.close()


class SweepTests(unittest.TestCase):
    def test_all_six_isolation_routes_and_if_source_frequency(self):
        for method, (source, output) in ISOLATION_PORTS.items():
            with self.subTest(method=method):
                settings = dict(SETTINGS, sweep_kind="fixed", if_loss_db=3)
                config, point, plan = plan_sequence(AnalyzerConfig(), dict(CONTEXT, method=method), settings, 1)[0]
                expected_frequency = point[source.lower() + "_hz"]
                self.assertEqual(target_frequency(point), expected_frequency)
                self.assertEqual(config.center_hz, expected_frequency)
                enabled = (1,) if source == "LO" else (2,)
                self.assertEqual(active_channels(point, plan), enabled)
                self.assertEqual(plan.channels()[enabled[0]][0], expected_frequency)
                expected_pin = 11 if source == "LO" else -23 if source == "IF" else -21
                self.assertEqual(point["pin_dbm"], expected_pin)

    def test_extra_50mhz_band_is_supported(self):
        settings = dict(SETTINGS, sweep_kind="matrix", matrix_rf_mhz="50;51;52",
                        matrix_rf_dbm="-60;-50;-40;-30;-20;-10;0", matrix_lo_dbm=";".join(map(str, range(10, 21))))
        plans = plan_sequence(AnalyzerConfig(), CONTEXT, settings, 231)
        self.assertEqual(len(plans), 231)
        self.assertEqual({plan.rf_hz for _, _, plan in plans}, {50e6, 51e6, 52e6})
        self.assertEqual({plan.lo_hz - plan.rf_hz for _, _, plan in plans}, {11059200})

    def test_matrix_is_cartesian_product_with_no_missing_combinations(self):
        options = dict(SETTINGS, sweep_kind="matrix", matrix_rf_mhz="1,85;14;28.5",
                       matrix_rf_dbm="−40;-20;0", matrix_lo_dbm="7;13")
        plans = plan_sequence(AnalyzerConfig(), CONTEXT, options, 1)
        self.assertEqual(sequence_count(options, 1), 18)
        combinations = {(plan.rf_hz, plan.rf_dbm, plan.lo_dbm) for _, _, plan in plans}
        self.assertEqual(len(combinations), 18)
        self.assertEqual(combinations, {(rf, level, lo) for rf in (1.85e6, 14e6, 28.5e6)
                                        for level in (-40, -20, 0) for lo in (7, 13)})
        for _, context, plan in plans:
            self.assertEqual(context["sweep_kind"], "matrix")
            self.assertEqual(plan.lo_hz - plan.rf_hz, 11059200)
        for key, value in (("matrix_rf_dbm", "0;0"), ("matrix_lo_dbm", "nan"), ("matrix_rf_mhz", "")):
            with self.assertRaises(ValueError):
                matrix_axes(dict(options, **{key: value}))

    def test_if_tracking_both_presets_lower_lo_and_calibration(self):
        for intermediate in (11059200, 8192000):
            context = dict(CONTEXT, if_hz=intermediate, lo_hz=14e6 + intermediate)
            plans = plan_sequence(AnalyzerConfig(), context, SETTINGS, 3)
            self.assertEqual([plan.rf_hz for _, _, plan in plans], [14e6, 14.5e6, 15e6])
            for config, point, plan in plans:
                self.assertEqual(plan.lo_hz - plan.rf_hz, intermediate)
                self.assertEqual(config.center_hz, intermediate)
                self.assertEqual(point["pin_dbm"], -21)
                self.assertEqual(active_channels(point, plan), (1, 2))
            context["lo_hz"] = context["rf_hz"] - intermediate
            for _, _, plan in plan_sequence(AnalyzerConfig(), context, SETTINGS, 3):
                self.assertEqual(plan.rf_hz - plan.lo_hz, intermediate)

    def test_isolation_only_enables_injected_channel_and_checks_whole_range(self):
        for method, enabled, pin in (("lo_if", (1,), 11), ("rf_if", (2,), -21)):
            _, context, plan = plan_sequence(AnalyzerConfig(), dict(CONTEXT, method=method), SETTINGS, 3)[0]
            self.assertEqual(active_channels(context, plan), enabled)
            self.assertEqual(context["pin_dbm"], pin)
        for options in (dict(SETTINGS, sweep_kind="rf_power", sweep_start=-20, sweep_stop=21),
                        dict(SETTINGS, sweep_start=3, sweep_stop=61)):
            with self.assertRaises(ValueError):
                plan_sequence(AnalyzerConfig(), CONTEXT, options, 3)
        with self.assertRaises(ValueError):
            plan_sequence(AnalyzerConfig(), dict(CONTEXT, lo_hz=2940800), dict(SETTINGS, sweep_start=3), 3)


class FakeAnalyzer:
    def __init__(self, host, log):
        self.identity = "SIMULATED RSA HARDWARE INTERFACE"
        self.config = AnalyzerConfig()
        self.failure = None

    def configure(self, config):
        self.config = config
        return {}

    def acquire(self, stop):
        if self.failure:
            raise self.failure
        result = demo_spectrum(self.config)
        result["demo"] = False
        return result

    def close(self):
        pass


class SequenceWorkerTests(unittest.TestCase):
    def run_worker(self, failure=None, rf_limit=20):
        driver, device = make_driver()
        device.states[2]["power_limit_dbm"] = rf_limit
        analyzer = FakeAnalyzer("", None)
        analyzer.failure = failure
        options = dict(SETTINGS)
        if rf_limit == 0:
            options.update(sweep_kind="rf_power", sweep_start=-20, sweep_stop=10)
        worker = RsaWorker("127.0.0.1", False, "series", AnalyzerConfig(), CONTEXT, 3, 0, None, options, averages=3)
        spectra, errors, states = [], [], []
        worker.spectrum.connect(spectra.append)
        worker.failed.connect(errors.append)
        worker.generator_state.connect(states.append)
        with patch("gsmbench.rsa_ui.Smu200a", return_value=driver), patch("gsmbench.rsa_ui.Rsa6265", return_value=analyzer):
            worker.run()
        return device, spectra, errors, states

    def test_successful_sweep_retunes_without_output_cycling_and_saves_source(self):
        device, spectra, errors, states = self.run_worker()
        self.assertFalse(errors, errors)
        self.assertEqual(len(spectra), 3)
        self.assertEqual(spectra[-1]["context"]["rf_hz"], 15e6)
        self.assertEqual(spectra[-1]["context"]["rf_channel"], 2)
        self.assertEqual(spectra[-1]["generator"]["channels"]["1"]["output"], 1)
        self.assertEqual(sum(command.startswith(":OUTP") and command.endswith(" ON") for command in device.commands), 2)
        first_on = next(index for index, command in enumerate(device.commands) if command.startswith(":OUTP") and command.endswith(" ON"))
        last_off = next(index for index in range(first_on, len(device.commands)) if device.commands[index] == ":OUTP1:STAT OFF")
        self.assertGreater(last_off, first_on)
        self.assertTrue(states[-1]["known"])
        self.assertTrue(all(not channel["output"] for channel in states[-1]["state"]["channels"].values()))

    def test_rsa_failure_and_stop_switch_off_both_generator_outputs(self):
        for failure in (TimeoutError("RSA failed"), AcquisitionCancelled()):
            device, spectra, errors, states = self.run_worker(failure)
            self.assertFalse(spectra)
            self.assertTrue(all(not channel["output"] for channel in device.states.values()))
            self.assertTrue(states[-1]["known"])

    def test_late_hardware_limit_violation_prevents_any_rf_on(self):
        device, spectra, errors, states = self.run_worker(rf_limit=0)
        self.assertTrue(errors)
        self.assertFalse(spectra)
        self.assertFalse(any(command.startswith(":OUTP") and command.endswith(" ON") for command in device.commands))


if __name__ == "__main__":
    unittest.main()
