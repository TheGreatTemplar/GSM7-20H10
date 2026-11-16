"""Dedicated mixer database with spectrum UUID exchange; refuses other applications' tables."""

from contextlib import closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import uuid


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
                db.commit()

    def check_tables(self, db):
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables - {"rf_spectrum", "sqlite_sequence"}:
            raise ValueError("Файл содержит таблицы другой программы. Выберите отдельную базу смесителей.")

    def save_many(self, spectra):
        for spectrum in spectra:
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
                    if json.loads(old[0]) != json.loads(payload):
                        raise ValueError("Конфликт UUID спектра: " + spectrum["id"])
                    continue
                db.execute("INSERT INTO rf_spectrum VALUES (?, ?)", (spectrum["id"], payload))
                added += 1
        return added

    def load(self):
        if not self.path.exists():
            return []
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
            self.check_tables(db)
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='rf_spectrum'").fetchone():
                return []
            spectra = (json.loads(row[0]) for row in db.execute("SELECT payload FROM rf_spectrum ORDER BY rowid DESC"))
            return [spectrum for spectrum in spectra if spectrum["demo"] == self.demo]

    def export_json(self, path):
        Path(path).write_text(json.dumps(dict(format="rsa-spectrum-1", spectra=self.load()),
                                        ensure_ascii=False, indent=4, allow_nan=False), encoding="utf-8")

    def import_json(self, path):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("format") != "rsa-spectrum-1" or not isinstance(payload.get("spectra"), list):
            raise ValueError("Неизвестный формат архива спектров")
        return self.save_many(payload["spectra"])
