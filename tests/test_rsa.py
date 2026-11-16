import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock

from gsmbench.rf_storage import SpectrumStore
from gsmbench.rsa import (AcquisitionCancelled, AnalyzerConfig, Rsa6265,
                          demo_spectrum, frequencies, mixer_result, parse_trace, peak_near)


class FakeRsa:
    def __init__(self):
        self.spectrum = demo_spectrum(AnalyzerConfig())
        self.commands = []
        self.esr_reads = 0
        self.complete = True
        self.cancel = None
        self.timeout = None

    def query(self, command):
        self.commands.append(command)
        if ";" in command:
            return ";".join(self.query(part) for part in command.split(";"))
        if command == "*IDN?":
            return "RIGOL TECHNOLOGIES,RSA6265,123,1.0"
        if command == ":INIT:CONT?":
            return "1"
        if command == ":SYST:ERR?":
            return '+0,"No error"'
        if command == ":INP:IF:OVER?":
            return "0"
        if command == "*ESR?":
            self.esr_reads += 1
            if self.esr_reads >= 2 and self.cancel:
                self.cancel.set()
            return "1" if self.complete and self.esr_reads >= 2 else "0"
        if command == ":TRAC:DATA? TRACE1":
            return ",".join(str(value) for value in self.spectrum["power_dbm"])
        for name, (query, cast) in Rsa6265.METADATA.items():
            if command == query:
                return str(self.spectrum["settings"][name])
        raise AssertionError("Unexpected query: " + command)

    def write(self, command):
        self.commands.append(command)

    def close(self):
        pass


class RsaTests(unittest.TestCase):
    def driver(self):
        device = FakeRsa()
        manager = Mock()
        manager.open_resource.return_value = device
        return Rsa6265("172.16.236.90", manager_factory=Mock(return_value=manager)), device

    def test_trace_and_frequency_units(self):
        spectrum = demo_spectrum(AnalyzerConfig())
        hz = frequencies(spectrum)
        self.assertEqual(hz[0], 10959200)
        self.assertEqual(hz[-1], 11159200)
        marker = peak_near(spectrum, 11059200, 3000)
        self.assertEqual(marker["frequency_hz"], 11059200)
        self.assertEqual(marker["power_dbm"], -27)
        for text in ("#9000000001x", "nan," * 101, "1,2,3"):
            with self.assertRaises(ValueError):
                parse_trace(text)
        with self.assertRaises(ValueError):
            peak_near(spectrum, hz[0], 3000)

    def test_current_trace_is_read_only_and_single_restores_continuous(self):
        driver, device = self.driver()
        current = driver.read_current()
        self.assertEqual(current["acquisition"], "current_trace")
        self.assertFalse(any("?" not in command for command in device.commands))
        fresh = driver.acquire(threading.Event())
        self.assertEqual(fresh["acquisition"], "single_sweep")
        self.assertEqual(len(fresh["power_dbm"]), 801)
        self.assertEqual(device.commands[-3:], [":ABOR", ":INIT:CONT 1", ":INIT:CONT?"])
        driver.close()

    def test_stop_and_timeout_restore_continuous_without_fetch(self):
        for cancel in (True, False):
            driver, device = self.driver()
            stop = threading.Event()
            device.complete = False
            device.cancel = stop if cancel else None
            with self.assertRaises(AcquisitionCancelled if cancel else TimeoutError):
                driver.acquire(stop, timeout=10 if cancel else 0)
            self.assertEqual(device.commands[-3:], [":ABOR", ":INIT:CONT 1", ":INIT:CONT?"])
            self.assertNotIn(":TRAC:DATA? TRACE1", device.commands)
            driver.close()
        driver, device = self.driver()
        stop = threading.Event()
        stop.set()
        with self.assertRaises(AcquisitionCancelled):
            driver.acquire(stop)
        self.assertNotIn(":INIT", device.commands)
        driver.close()

    def test_preliminary_result_and_missing_reference_resolution_corrections(self):
        spectrum = demo_spectrum(AnalyzerConfig())
        spectrum["context"] = dict(method="conversion", pin_known=True, path_known=True,
            pin_dbm=-20, output_loss_db=2, if_hz=11059200, marker_window_hz=3000)
        result = mixer_result(spectrum)
        self.assertEqual(result["status"], "preliminary")
        self.assertEqual(result["value_db"], 5)
        for field, value in (("pin_known", False), ("path_known", False)):
            bad = copy.deepcopy(spectrum)
            bad["context"][field] = value
            self.assertEqual(mixer_result(bad)["status"], "not_calculated")
        for field, value in (("rbw_hz", 10), ("reference_offset_db", 3),
                             ("amplitude_correction", 1), ("trace_type", "MAXH"), ("reference_dbm", -30)):
            bad = copy.deepcopy(spectrum)
            bad["settings"][field] = value
            self.assertEqual(mixer_result(bad)["status"], "not_calculated")

    def test_wrong_mode_incomplete_trace_and_invalid_config_rejected(self):
        driver, device = self.driver()
        device.spectrum["settings"]["mode"] = "RTSA"
        with self.assertRaises(ValueError):
            driver.read_current()
        device.spectrum["settings"]["mode"] = "SA"
        device.spectrum["power_dbm"].pop()
        with self.assertRaises(ValueError):
            driver.read_current()
        before = list(device.commands)
        with self.assertRaises(ValueError):
            driver.configure(AnalyzerConfig(points=10))
        self.assertEqual(before, device.commands)
        driver.close()


class SpectrumStoreTests(unittest.TestCase):
    def test_separate_database_exchange_and_conflict_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SpectrumStore(root / "mixers-demo.db", True)
            spectrum = demo_spectrum(AnalyzerConfig())
            self.assertEqual(store.save_many([spectrum]), 1)
            self.assertEqual(store.save_many([spectrum]), 0)
            archive = root / "exchange.json"
            store.export_json(archive)
            target = SpectrumStore(root / "second-demo.db", True)
            self.assertEqual(target.import_json(archive), 1)
            self.assertEqual(target.import_json(archive), 0)
            changed = copy.deepcopy(spectrum)
            changed["power_dbm"][0] += 1
            with self.assertRaises(ValueError):
                target.save_many([demo_spectrum(AnalyzerConfig()), changed])
            self.assertEqual(len(target.load()), 1)
            real = SpectrumStore(root / "mixers.db", False)
            with self.assertRaises(ValueError):
                real.import_json(archive)
            self.assertFalse(real.path.exists())

    def test_quartz_and_diode_database_refused_and_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "other.db"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE gsm_run (id TEXT)")
                db.execute("INSERT INTO gsm_run VALUES ('preserve')")
            store = SpectrumStore(path, False)
            with self.assertRaises(ValueError):
                store.initialize()
            with self.assertRaises(ValueError):
                store.load()
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT * FROM gsm_run").fetchall(), [("preserve",)])
                self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name='rf_spectrum'").fetchone())

    def test_invalid_import_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            spectrum = demo_spectrum(AnalyzerConfig())
            spectrum["power_dbm"].pop()
            archive = path / "invalid.json"
            archive.write_text(json.dumps(dict(format="rsa-spectrum-1", spectra=[spectrum])), encoding="utf-8")
            store = SpectrumStore(path / "new.db", True)
            with self.assertRaises(ValueError):
                store.import_json(archive)
            self.assertFalse(store.path.exists())


if __name__ == "__main__":
    unittest.main()
