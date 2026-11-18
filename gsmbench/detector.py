"""LO-detector calibration tables, source adapters and a separate SQLite archive."""

from bisect import bisect_left
from contextlib import closing
from dataclasses import dataclass
import json
import math
from pathlib import Path
import sqlite3
import statistics
import uuid

from .smu import Smu200a, SourcePlan, verified_off
from .ssg import Ssg3000x, verified_ssg_off
from .validation import FieldError


@dataclass(frozen=True)
class DetectorPlan:
    frequency_hz: float = 14000000
    start_dbm: float = -30
    stop_dbm: float = 10
    step_db: float = 1
    input_attenuation_db: float = 0
    settling_s: float = 0.5
    samples: int = 3
    nplc: float = 10
    voltage_range: float = 10
    high_impedance: bool = True
    digits: float = 6.5

    def levels(self, single=False):
        values = (self.frequency_hz, self.start_dbm, self.stop_dbm, self.step_db, self.input_attenuation_db, self.settling_s)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Параметры должны быть конечными")
        if not 100000 <= self.frequency_hz <= 3e9:
            raise FieldError("Частота калибровки: 0,1…3000 МГц", "frequency_mhz")
        if not -110 <= self.start_dbm <= 20 or not -110 <= self.stop_dbm <= 20:
            raise FieldError("Уровень источника: −110…+20 dBm, с проверкой прибором", "start_dbm", "stop_dbm")
        if not 0 < self.step_db <= 20 or (not single and self.stop_dbm < self.start_dbm):
            raise FieldError("Нужны возрастающий диапазон и положительный шаг до 20 дБ", "start_dbm", "stop_dbm", "step_db")
        if not 0 <= self.input_attenuation_db <= 100 or not 0 <= self.settling_s <= 60:
            raise FieldError("Ослабление: 0…100 дБ; установление: 0…60 с", "attenuation_db", "settling_s")
        if type(self.samples) is not int or not 1 <= self.samples <= 100:
            raise ValueError("Усреднение: 1…100 измерений")
        if self.nplc not in (0.02, 0.2, 1, 10, 100) or self.voltage_range not in (0, 0.1, 1, 10, 100, 1000):
            raise ValueError("Неподдерживаемый NPLC или диапазон вольтметра")
        if self.digits not in (4.5, 5.5, 6.5):
            raise ValueError("Разрядность 34401A: 4,5; 5,5 или 6,5")
        count = 1 if single else math.ceil((self.stop_dbm - self.start_dbm) / self.step_db) + 1
        if count > 401:
            raise FieldError("В серии допускается до 401 точки", "start_dbm", "stop_dbm", "step_db")
        return [min(self.stop_dbm, self.start_dbm + index * self.step_db) for index in range(count)]


def voltage_to_dbm(points, voltage):
    if not math.isfinite(voltage) or len(points) < 2:
        raise ValueError("Нужны минимум две точки и конечное напряжение")
    pairs = sorted((point["input_dbm"], point["voltage_v"]) for point in points)
    if len({power for power, _ in pairs}) != len(pairs):
        raise ValueError("Для пересчёта выберите одну кривую без повторных уровней")
    differences = [b[1] - a[1] for a, b in zip(pairs, pairs[1:])]
    if not (all(value > 0 for value in differences) or all(value < 0 for value in differences)):
        raise ValueError("Кривая немонотонна: напряжение не задаёт мощность однозначно")
    by_voltage = sorted((value, power) for power, value in pairs)
    if not by_voltage[0][0] <= voltage <= by_voltage[-1][0]:
        raise ValueError("Напряжение вне измеренного диапазона; экстраполяция запрещена")
    index = bisect_left([value for value, _ in by_voltage], voltage)
    if index == 0:
        return by_voltage[0][1]
    low, high = by_voltage[index - 1], by_voltage[index]
    return low[1] + (voltage - low[0]) * (high[1] - low[1]) / (high[0] - low[0])


class AkipDetectorSource:
    def __init__(self, settings, log):
        self.driver = Ssg3000x(settings["akip_host"], settings["akip_port"], log)

    def prepare(self, frequency, level):
        return self.driver.prepare(frequency, level)

    def setpoint(self, frequency, level):
        return self.driver.setpoint(frequency, level)

    def enable(self, stop):
        return self.driver.enable(stop)

    def state(self):
        return self.driver.state()

    def off(self):
        return verified_ssg_off(self.driver)

    def close(self):
        self.driver.close()


class SmuDetectorSource:
    def __init__(self, settings, log):
        self.channel = 1 if settings["source"] == "smu_a" else 2
        self.driver = Smu200a(settings["smu_host"], settings["smu_port"], log)

    def plan(self, frequency, level):
        return SourcePlan(frequency, frequency, level, -110, self.channel, 3 - self.channel)

    def flat(self, state):
        selected = state["channels"][str(self.channel)]
        if state["channels"][str(3 - self.channel)]["output"]:
            raise ValueError("Неиспользуемый выход SMU должен быть OFF")
        return dict(identity=state["identity"], frequency_hz=selected["frequency_hz"], level_dbm=selected["level_dbm"],
            output=selected["output"], channel=self.channel)

    def prepare(self, frequency, level):
        return self.flat(self.driver.prepare(self.plan(frequency, level)))

    def setpoint(self, frequency, level):
        return self.flat(self.driver.setpoint(self.plan(frequency, level)))

    def enable(self, stop):
        return self.flat(self.driver.enable((self.channel,), stop))

    def state(self):
        return self.flat(self.driver.state())

    def off(self):
        return self.flat(verified_off(self.driver))

    def close(self):
        self.driver.close()


# New generators (e.g. a serial SMR20) implement this same adapter interface.
SOURCE_FACTORIES = {"akip": AkipDetectorSource, "smu_a": SmuDetectorSource, "smu_b": SmuDetectorSource}


class DetectorStore:
    def __init__(self, path, demo):
        self.path, self.demo = Path(path).resolve(), demo

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables - {"detector_series", "sqlite_sequence"}:
            db.close()
            raise ValueError("Выберите отдельную БД калибровок детекторов")
        db.execute("CREATE TABLE IF NOT EXISTS detector_series (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        db.commit()
        return db

    def validate(self, run):
        uuid.UUID(run["id"])
        if type(run["demo"]) is not bool or run["demo"] != self.demo:
            raise ValueError("Демо и реальные калибровки хранятся отдельно")
        if not isinstance(run["sample"], str) or not run["sample"].strip():
            raise ValueError("Отсутствует номер детектора")
        plan = DetectorPlan(**run["plan"])
        levels = plan.levels(single=run["operation"] == "point")
        if run["state"] not in ("running", "complete", "cancelled", "failed") or len(run["points"]) > len(levels):
            raise ValueError("Некорректное состояние серии")
        if run["state"] == "complete" and len(run["points"]) != len(levels):
            raise ValueError("Неполная завершённая серия")
        for index, point in enumerate(run["points"]):
            values = point["readings_v"]
            if point["index"] != index + 1 or len(values) != plan.samples or not all(math.isfinite(value) and abs(value) < 1e9 for value in values):
                raise ValueError("Некорректные отсчёты вольтметра")
            if (not math.isclose(point["set_dbm"], levels[index], abs_tol=1e-6)
                    or not math.isclose(point["actual_dbm"], point["set_dbm"], abs_tol=0.011)
                    or not math.isclose(point["input_dbm"], point["actual_dbm"] - plan.input_attenuation_db, abs_tol=1e-9)
                    or not math.isclose(point["voltage_v"], statistics.mean(values), abs_tol=1e-12)):
                raise ValueError("Несогласованные уровни или среднее напряжение")
        json.dumps(run, allow_nan=False)

    def save(self, run):
        self.save_many([run], allow_update=True)

    def save_many(self, runs, allow_update=False):
        for run in runs:
            self.validate(run)
        with closing(self.connect()) as db, db:
            for run in runs:
                payload = json.dumps(run, ensure_ascii=False, allow_nan=False)
                old = db.execute("SELECT payload FROM detector_series WHERE id=?", (run["id"],)).fetchone()
                if old:
                    previous = json.loads(old[0])
                    if previous == run:
                        continue
                    if not allow_update or previous["state"] != "running":
                        raise ValueError("Конфликт UUID калибровки: " + run["id"])
                db.execute("INSERT INTO detector_series VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (run["id"], payload))

    def load(self):
        if not self.path.exists():
            return []
        with closing(self.connect()) as db:
            return [run for raw, in db.execute("SELECT payload FROM detector_series ORDER BY rowid DESC")
                if (run := json.loads(raw))["demo"] == self.demo]
