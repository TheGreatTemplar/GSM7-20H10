"""Dedicated mixer database with spectrum UUID exchange; refuses other applications' tables."""

from contextlib import closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import uuid

from .rsa import mixer_result
from .uncertainty import result_uncertainty


def validate_spectrum(spectrum):
    uuid.UUID(spectrum["id"])
    if type(spectrum["demo"]) is not bool or not isinstance(spectrum["identity"], str):
        raise ValueError("Некорректный паспорт спектра")
    datetime.fromisoformat(spectrum["measured_at"])
    settings = spectrum["settings"]
    powers = spectrum["power_dbm"]
    if settings["mode"] != "SA" or settings["unit"] != "DBM":
        raise ValueError("Архив поддерживает спектры SA в dBm")
    if not math.isfinite(settings["start_hz"]) or not math.isfinite(settings["stop_hz"]) or not 0 <= settings["start_hz"] < settings["stop_hz"] <= 26.5e9:
        raise ValueError("Недопустимый диапазон спектра")
    if not 101 <= len(powers) <= 100001 or len(powers) != settings["points"]:
        raise ValueError("Неполный спектр")
    if any(not math.isfinite(value) or abs(value) > 1000 for value in powers):
        raise ValueError("Недостоверные отсчёты спектра")
    json.dumps(spectrum, allow_nan=False)


def add_result_uncertainty(spectrum):
    result = mixer_result(spectrum)
    result["uncertainty"] = result_uncertainty(spectrum, result)
    spectrum["result"] = result
    return spectrum


class SpectrumStore:
    def __init__(self, path, demo):
        self.path = Path(path).expanduser().resolve()
        self.demo = demo

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            self.check_tables(db)
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='rf_spectrum'").fetchone():
                db.execute("CREATE TABLE rf_spectrum (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS rf_calibration (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.commit()

    def check_tables(self, db):
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables - {"rf_spectrum", "rf_calibration", "sqlite_sequence"}:
            raise ValueError("Файл содержит таблицы другой программы. Выберите отдельную базу смесителей.")

    def save_many(self, spectra):
        for spectrum in spectra:
            add_result_uncertainty(spectrum)
            validate_spectrum(spectrum)
            if spectrum["demo"] != self.demo:
                raise ValueError("Демо и реальные спектры сохраняются отдельно")
        self.initialize()
        added = 0
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            for spectrum in spectra:
                payload = json.dumps(spectrum, ensure_ascii=False, sort_keys=True, allow_nan=False)
                old = db.execute("SELECT payload FROM rf_spectrum WHERE id=?", (spectrum["id"],)).fetchone()
                if old:
                    old_spectrum = json.loads(old[0])
                    if "uncertainty" not in old_spectrum.get("result", {}):
                        add_result_uncertainty(old_spectrum)
                    if old_spectrum != json.loads(payload):
                        raise ValueError("Конфликт UUID спектра: " + spectrum["id"])
                    db.execute("UPDATE rf_spectrum SET payload=? WHERE id=?", (payload, spectrum["id"]))
                    continue
                db.execute("INSERT INTO rf_spectrum VALUES (?, ?)", (spectrum["id"], payload))
                added += 1
        return added

    def save_calibration(self, profile):
        uuid.UUID(profile["id"])
        if type(profile.get("demo")) is not bool or profile["demo"] != self.demo:
            raise ValueError("Демо и реальные калибровки сохраняются отдельно")
        payload = json.dumps(profile, ensure_ascii=False, sort_keys=True, allow_nan=False)
        self.initialize()
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("INSERT INTO rf_calibration VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (profile["id"], payload))

    def load_calibrations(self):
        if not self.path.exists():
            return []
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
            self.check_tables(db)
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='rf_calibration'").fetchone():
                return []
            calibrations = (json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM rf_calibration ORDER BY rowid DESC"))
            return [profile for profile in calibrations if profile.get("demo") == self.demo]

    def count(self):
        if not self.path.exists():
            return 0
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
            self.check_tables(db)
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='rf_spectrum'").fetchone():
                return 0
            return db.execute("SELECT COUNT(*) FROM rf_spectrum").fetchone()[0]

    def load(self):
        if not self.path.exists():
            return []
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            self.check_tables(db)
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='rf_spectrum'").fetchone():
                return []
            spectra = []
            for identity, payload in db.execute("SELECT id, payload FROM rf_spectrum ORDER BY rowid DESC"):
                spectrum = json.loads(payload)
                if spectrum["demo"] != self.demo:
                    continue
                if "uncertainty" not in spectrum.get("result", {}):
                    add_result_uncertainty(spectrum)
                    db.execute("UPDATE rf_spectrum SET payload=? WHERE id=?", (
                        json.dumps(spectrum, ensure_ascii=False, sort_keys=True, allow_nan=False), identity))
                spectra.append(spectrum)
            return spectra

    def export_json(self, path):
        Path(path).write_text(json.dumps(dict(format="rsa-spectrum-1", spectra=self.load(),
            calibrations=self.load_calibrations()),
                                        ensure_ascii=False, indent=4, allow_nan=False), encoding="utf-8")

    def import_json(self, path):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("format") != "rsa-spectrum-1" or not isinstance(payload.get("spectra"), list):
            raise ValueError("Неизвестный формат архива спектров")
        added = self.save_many(payload["spectra"])
        for profile in payload.get("calibrations", []):
            self.save_calibration(profile)
        return added
