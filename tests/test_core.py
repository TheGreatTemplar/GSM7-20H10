import copy
from contextlib import closing
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from gsmbench.core import Point, Recipe, generate_levels, match_groups, parse_reading, simulate
from gsmbench.instrument import Instrument
from gsmbench.storage import Store, new_run


def make_run(sample="D001", offset=0):
    recipe = Recipe()
    run = new_run(sample, "1N5711", "lot1", recipe, True, "SIMULATOR")
    run["points"] = [asdict(Point(p.setpoint, p.voltage + offset, p.current)) for p in simulate(recipe, 1)]
    run["state"] = "complete"
    # Persisted representation is canonical JSON (tuples become arrays).
    return json.loads(json.dumps(run))


class FakeTransport:
    def __init__(self, bad_read=False, bad_setup=False):
        self.commands = []
        self.bad_read = bad_read
        self.bad_setup = bad_setup
        self.levels = []
        self.started = False
        self.stop_on_poll = None
        self.short_buffer = False
        self.negative_voltage = False
        self.bad_status = False
        self.terminals = "FRON"
        self.remote_sense = "0"

    def write(self, command):
        self.commands.append(command)
        if command.startswith(":ROUT:TERM "):
            self.terminals = command.split()[1]
        if command.startswith(":SYST:RSEN "):
            self.remote_sense = "1" if command.endswith("ON") else "0"
        if command.startswith((":SOUR:LIST:CURR ", ":SOUR:LIST:VOLT ")):
            self.levels = command.split(" ", 1)[1].split(",")
        elif command.startswith((":SOUR:LIST:CURR:APP ", ":SOUR:LIST:VOLT:APP ")):
            self.levels += command.split(" ", 1)[1].split(",")
        elif command == ":INIT":
            self.started = True

    def query(self, command, **kwargs):
        self.commands.append(command)
        if command == ":ROUT:TERM?":
            return self.terminals
        if command == ":SYST:RSEN?":
            return self.remote_sense
        if command == ":FETC?":
            if self.bad_read:
                raise TimeoutError("Simulated connection failure")
            voltage = "-0.7" if self.negative_voltage else "0.4"
            status = "+9.910000e+37" if self.bad_status else "+3.788800e+04"
            return ",".join([f"{voltage},0.001,{status}"] * (len(self.levels) - int(self.short_buffer)))
        if command == ":SOUR:SWE:RANG?":
            return "AUTO"
        if command == ":SOUR:CLE:AUTO:MODE?":
            return "TCOUNT"
        if command == ":TRAC:POIN:ACT?":
            if self.started and self.stop_on_poll is not None:
                self.stop_on_poll.set()
                return "0"
            return str(len(self.levels) if self.started else 0)
        if command in (":SOUR:LIST:CURR:POIN?", ":SOUR:LIST:VOLT:POIN?", ":TRAC:POIN?", ":TRIG:COUN?"):
            return str(len(self.levels))
        if command == ":SYST:ERR?":
            return '-100,"Bad command"' if self.bad_setup else '0,"No error"'
        return "1" if command == "*OPC?" else "0"


class CoreTests(unittest.TestCase):
    def setUp(self):
        # Do not spend real acquisition time on the fake instrument.
        self.wait_patch = patch.object(Instrument, "wait_series", return_value=False)
        self.wait_patch.start()
        self.addCleanup(self.wait_patch.stop)

    def test_stop_before_completion_query_sends_abort_without_query(self):
        stop = threading.Event()
        transport = FakeTransport()

        def wait(recipe, event):
            event.set()
            return True

        with patch.object(Instrument, "wait_series", side_effect=wait):
            Instrument(transport).measure(Recipe(), stop, lambda point: self.fail("Cancelled data accepted"))
        acquisition = transport.commands[transport.commands.index(":INIT") + 1:]
        self.assertEqual(acquisition, [":ABOR", ":OUTP OFF", ":OUTP OFF", ":OUTP?"])

    def test_reset_with_firmware_returning_zero_to_opc(self):
        transport = FakeTransport()
        with patch("gsmbench.instrument.time.sleep") as settle:
            Instrument(transport).reset()
        settle.assert_called_once_with(1.0)
        self.assertNotIn("*OPC?", transport.commands)
        self.assertNotIn("*WAI", transport.commands)
        self.assertLess(transport.commands.index("*RST"), transport.commands.index(":SYST:ERR?"))
        self.assertEqual(transport.commands[-2:], [":OUTP OFF", ":OUTP?"])

    def test_generated_ranges(self):
        for spacing in ("linear", "log"):
            points = generate_levels(1e-6, 0.011, 20, spacing)
            self.assertEqual(len(points), 20)
            self.assertEqual((points[0], points[-1]), (1e-6, 0.011))
            self.assertTrue(all(a < b for a, b in zip(points, points[1:])))
            steps = [b - a if spacing == "linear" else b / a for a, b in zip(points, points[1:])]
            for step in steps:
                self.assertAlmostEqual(step, steps[0], places=12)

    def test_invalid_generated_range(self):
        for args in [(0, 1), (2, 1), (1, float("inf")), (1, 2, 1), (1, 2, 20, "other")]:
            with self.assertRaises(ValueError):
                generate_levels(*args)

    def test_rejects_nonfinite_and_excess_power(self):
        for recipe in [Recipe(levels=(float("nan"),)), Recipe(levels=(0.10001,)),
                       Recipe("j310", (10,), 0.08), Recipe(levels=(0.001, 0.001))]:
            with self.assertRaises(ValueError):
                recipe.validate()

    def test_100ma_and_connection_conditions(self):
        Recipe(levels=(1e-6, 0.1)).validate()
        Recipe("j310", (1, 2, 3), 0.1).validate()
        for wires in (2, 4):
            for terminals in ("front", "rear"):
                recipe = Recipe(wires=wires, terminals=terminals)
                transport = FakeTransport()
                Instrument(transport).measure(recipe, threading.Event(), lambda point: None)
                self.assertIn(":SYST:RSEN " + ("ON" if wires == 4 else "OFF"), transport.commands)
                self.assertIn(":ROUT:TERM " + ("FRON" if terminals == "front" else "REAR"), transport.commands)

    def test_connection_conditions_prevent_mixed_matching(self):
        for key, value in (("wires", 4), ("terminals", "rear")):
            first, second = make_run("D1"), make_run("D2")
            second["recipe"][key] = value
            self.assertFalse(match_groups([first, second], 2, 0.01))

    def test_bad_measurements(self):
        for value in ["0.4,0.001,8", "nan,1,0", "9.9e37,1,0", "1,2", "1,2,0.5", "1,2,65536"]:
            with self.assertRaises(ValueError):
                parse_reading(value, 0.001)

    def test_matching_all_points(self):
        runs = [make_run("D1"), make_run("D2", 0.002), make_run("D3", 0.003), make_run("D4", 0.004)]
        self.assertEqual(len(match_groups(runs, 4, 0.005)), 1)
        runs[3]["points"][0]["voltage"] += 0.02
        self.assertFalse(match_groups(runs, 4, 0.005))

    def test_incompatible_and_failed_runs_excluded(self):
        for key, value in [("state", "failed"), ("sample", "D1"), ("demo", False), ("model", "other")]:
            left, right = make_run("D1"), make_run("D2")
            right[key] = value
            self.assertFalse(match_groups([left, right], 2, 0.01))

    def test_output_off_after_timeout(self):
        transport = FakeTransport(bad_read=True)
        with self.assertRaises(TimeoutError):
            Instrument(transport).measure(Recipe(), threading.Event(), lambda point: None)
        self.assertEqual(transport.commands[-2:], [":OUTP OFF", ":OUTP?"])

    def test_setup_error_never_starts_measurement(self):
        transport = FakeTransport(bad_setup=True)
        with self.assertRaises(RuntimeError):
            Instrument(transport).measure(Recipe(), threading.Event(), lambda point: None)
        self.assertNotIn(":READ?", transport.commands)
        self.assertEqual(transport.commands[-2:], [":OUTP OFF", ":OUTP?"])

    def test_cancel_aborts_native_series(self):
        transport = FakeTransport()
        stop = threading.Event()
        transport.stop_on_poll = stop
        Instrument(transport).measure(Recipe(), stop, lambda point: self.fail("Cancelled buffer read"))
        self.assertIn(":ABOR", transport.commands)
        self.assertNotIn(":FETC?", transport.commands)
        self.assertEqual(transport.commands[-2:], [":OUTP OFF", ":OUTP?"])

    def test_native_series_single_start_and_buffer_read(self):
        transport = FakeTransport()
        recipe = Recipe(levels=generate_levels(1e-6, 0.011))
        points = []
        Instrument(transport).measure(recipe, threading.Event(), points.append)
        self.assertEqual([p.setpoint for p in points], list(recipe.levels))
        self.assertEqual(transport.commands.count(":INIT"), 1)
        self.assertEqual(transport.commands.count(":FETC?"), 1)
        self.assertNotIn(":TRAC:DATA?", transport.commands)
        self.assertIn(":SOUR:SWE:RANG AUTO", transport.commands)
        self.assertTrue(all(p.status == 37888 for p in points))
        self.assertNotIn(":READ?", transport.commands)
        self.assertNotIn(":SOUR:CLE:AUTO:MODE ALWAYS", transport.commands)
        self.assertIn(":SOUR:CLE:AUTO:MODE TCO", transport.commands)

    def test_incomplete_buffer_rejected(self):
        transport = FakeTransport()
        transport.short_buffer = True
        with self.assertRaisesRegex(ValueError, "Размер буфера"):
            Instrument(transport).measure(Recipe(), threading.Event(), lambda point: None)
        self.assertEqual(transport.commands[-2:], [":OUTP OFF", ":OUTP?"])

    def test_missing_status_is_not_silently_accepted(self):
        transport = FakeTransport()
        transport.bad_status = True
        with self.assertRaisesRegex(ValueError, "слово состояния"):
            Instrument(transport).measure(Recipe(), threading.Event(), lambda point: self.fail("Invalid point accepted"))

    def test_negative_forward_voltage_rejected(self):
        transport = FakeTransport()
        transport.negative_voltage = True
        with self.assertRaisesRegex(ValueError, "отрицательное Vf"):
            Instrument(transport).measure(Recipe(), threading.Event(), lambda point: self.fail("Invalid point accepted"))

    def test_long_list_uploaded_in_chunks(self):
        transport = FakeTransport()
        recipe = Recipe("j310", generate_levels(0.5, 5, 200), 0.05)
        Instrument(transport).measure(recipe, threading.Event(), lambda point: None)
        self.assertEqual(len(transport.levels), 200)
        self.assertTrue(any(command.startswith(":SOUR:LIST:VOLT:APP ") for command in transport.commands))


class StorageTests(unittest.TestCase):
    def test_backup_preservation_and_idempotent_import(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quartz.db"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE quartz (name TEXT)")
                db.execute("INSERT INTO quartz VALUES ('Q1')")
            store = Store(path)
            self.assertEqual(store.save_many([make_run()]), 1)
            self.assertEqual(len(list(Path(directory).glob("*.bak"))), 1)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT name FROM quartz").fetchall(), [("Q1",)])
            exchange = Path(directory) / "exchange.json"
            store.export_json(exchange)
            other = Store(Path(directory) / "other.db")
            self.assertEqual(other.import_json(exchange), 1)
            self.assertEqual(other.import_json(exchange), 0)

    def test_conflict_rolls_back_whole_import(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.db")
            original = make_run()
            store.save_many([original])
            conflict = copy.deepcopy(original)
            conflict["sample"] = "changed"
            with self.assertRaises(ValueError):
                store.save_many([make_run("D2"), conflict])
            self.assertEqual(len(store.load()), 1)

    def test_invalid_import_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.db")
            invalid = make_run()
            invalid["points"] = []
            with self.assertRaises(ValueError):
                store.save_many([invalid])
            self.assertFalse(store.path.exists())


if __name__ == "__main__":
    unittest.main()
