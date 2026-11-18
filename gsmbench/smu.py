"""Two-channel CW control for the verified SMU200A firmware 3.1 command set."""

from dataclasses import dataclass
import math
import re

from .rf_instruments import connection_error_message, socket_resource


@dataclass(frozen=True)
class SourcePlan:
    rf_hz: float
    lo_hz: float
    rf_dbm: float = -20
    lo_dbm: float = 13
    rf_channel: int = 2
    lo_channel: int = 1

    def validate(self):
        if {self.rf_channel, self.lo_channel} != {1, 2}:
            raise ValueError("RF и LO должны занимать разные выходы A/1 и B/2")
        for frequency, level in ((self.rf_hz, self.rf_dbm), (self.lo_hz, self.lo_dbm)):
            if not math.isfinite(frequency) or not 100000 <= frequency <= 3e9:
                raise ValueError("Для SMU-B103/B203 частота должна быть 100 кГц…3 ГГц")
            if not math.isfinite(level) or not -145 <= level <= 20:
                raise ValueError("Уровень CW должен быть в пределах −145…+20 dBm; учитывается также предел прибора")

    def channels(self):
        return {self.rf_channel: (self.rf_hz, self.rf_dbm), self.lo_channel: (self.lo_hz, self.lo_dbm)}


class Smu200a:
    def __init__(self, host, port=5025, log=lambda text: None, manager_factory=None):
        if manager_factory is None:
            import pyvisa
            manager_factory = pyvisa.ResourceManager
        self.host, self.port, self.log = host, port, log
        self.device = None
        self.broken = False
        self.bounds = {}
        self.prepared = False
        resource = socket_resource(host, port)
        self.manager = manager_factory("@py")
        try:
            self.device = self.manager.open_resource(resource, open_timeout=3000)
            self.device.timeout = 3000
            self.device.write_termination = "\n"
            self.device.read_termination = "\n"
            self.identity = self.query("*IDN?")
            fields = self.identity.split(",")
            if len(fields) < 2 or fields[1].strip().upper() != "SMU200A":
                raise ValueError("Ожидался SMU200A: " + self.identity)
            self.options = self.query("*OPT?")
            if not re.search(r"SMU-B2\d\d\b", self.options):
                raise ValueError("Не обнаружен второй RF-тракт SMU; проверьте комплектацию")
        except Exception as error:
            self.close()
            message = connection_error_message("SMU200A", f"{host}:{port}", error)
            if message:
                raise ConnectionError(message) from error
            raise

    def write(self, command):
        self.log("SMU > " + command)
        self.device.write(command)

    def query(self, command):
        if self.broken:
            raise ConnectionError("После ошибки обмена SMU требуется новое соединение")
        self.log("SMU > " + command)
        try:
            response = self.device.query(command).strip()
        except Exception:
            self.broken = True
            raise
        self.log("SMU < " + response)
        return response

    def check_error(self):
        response = self.query(":SYST:ERR?")
        if int(response.split(",", 1)[0]) != 0:
            raise RuntimeError("SMU: " + response)

    def state(self):
        fields = {
            "frequency_hz": ("FREQ:CW?", float), "level_dbm": ("POW:LEV:IMM:AMPL?", float),
            "frequency_mode": ("FREQ:MODE?", str), "power_mode": ("POW:MODE?", str),
            "modulation": ("MOD:ALL:STAT?", int), "multiplier": ("FREQ:MULT?", float),
            "frequency_offset_hz": ("FREQ:OFFS?", float), "power_offset_db": ("POW:OFFS?", float),
            "power_limit_dbm": ("POW:LIM?", float), "alc": ("POW:ALC:STAT?", str),
            "correction": ("CORR:STAT?", int),
        }
        commands = []
        for channel in (1, 2):
            commands.append(f":OUTP{channel}:STAT?")
            commands.extend(f":SOUR{channel}:" + command for command, _ in fields.values())
        values = self.query(";".join(commands)).split(";")
        if len(values) != len(commands):
            self.broken = True
            raise ValueError("Неполный пакет параметров SMU; требуется новое соединение")
        values = iter(values)
        channels = {}
        for channel in (1, 2):
            channels[str(channel)] = dict(output=int(next(values)),
                                         **{name: cast(next(values).strip()) for name, (_, cast) in fields.items()})
        return dict(identity=self.identity, options=self.options, channels=channels)

    def all_off(self):
        # Attempt both writes before any query, including after a failed read.
        errors = []
        for channel in (1, 2):
            try:
                self.write(f":OUTP{channel}:STAT OFF")
            except Exception as error:
                errors.append(str(error))
        for channel in (1, 2):
            try:
                if int(self.query(f":OUTP{channel}:STAT?")) != 0:
                    errors.append(f"Выход {channel} остался включён")
            except Exception as error:
                errors.append(str(error))
        if errors:
            raise RuntimeError("Отключение SMU не подтверждено. Проверьте RF ON на приборе. " + "; ".join(errors))

    def prepare(self, plan):
        plan.validate()
        self.all_off()
        self.check_error()
        for channel in (1, 2):
            prefix = f":SOUR{channel}:"
            for command in ("MOD:ALL:STAT OFF", "FREQ:MODE CW", "POW:MODE CW",
                            "FREQ:MULT 1", "FREQ:OFFS 0", "POW:OFFS 0", "CORR:STAT OFF", "POW:ALC:STAT ON"):
                self.write(prefix + command)
            self.bounds[channel] = dict(
                fmin=float(self.query(prefix + "FREQ:CW? MIN")),
                fmax=float(self.query(prefix + "FREQ:CW? MAX")),
                pmin=float(self.query(prefix + "POW? MIN")),
                pmax=min(float(self.query(prefix + "POW? MAX")), float(self.query(prefix + "POW:LIM?"))))
        self.check_error()
        self.prepared = True
        return self.setpoint(plan)

    def setpoint(self, plan):
        self.validate_plan(plan)
        for channel, (frequency, level) in plan.channels().items():
            self.write(f":SOUR{channel}:FREQ:CW {frequency:.12g}")
            self.write(f":SOUR{channel}:POW:LEV:IMM:AMPL {level:.12g}")
        self.check_error()
        state = self.state()
        self.verify(plan, state)
        return state

    def validate_plan(self, plan):
        plan.validate()
        if not self.prepared:
            raise RuntimeError("Сначала подготовьте CW при отключённых выходах")
        for channel, (frequency, level) in plan.channels().items():
            bounds = self.bounds[channel]
            if not bounds["fmin"] <= frequency <= bounds["fmax"] or not bounds["pmin"] <= level <= bounds["pmax"]:
                raise ValueError(f"Задание выхода {channel} превышает фактические пределы SMU: {bounds}")

    def verify(self, plan, state):
        for channel, (frequency, level) in plan.channels().items():
            actual = state["channels"][str(channel)]
            if (not math.isclose(actual["frequency_hz"], frequency, rel_tol=0, abs_tol=0.05) or
                    not math.isclose(actual["level_dbm"], level, rel_tol=0, abs_tol=0.011) or
                    actual["frequency_mode"] not in ("CW", "FIX") or actual["power_mode"] not in ("CW", "FIX") or
                    level > actual["power_limit_dbm"] or
                    actual["alc"] not in ("ON", "1") or actual["modulation"] or actual["correction"] or actual["multiplier"] != 1 or
                    actual["frequency_offset_hz"] or actual["power_offset_db"]):
                raise ValueError(f"SMU не подтвердил чистый CW и задание выхода {channel}: {actual}")

    def enable(self, channels, stop):
        if not self.prepared:
            raise RuntimeError("SMU не подготовлен")
        if not set(channels) <= {1, 2}:
            raise ValueError("Допустимы только выходы 1 и 2")
        for channel in channels:
            if stop.is_set():
                raise InterruptedError("Включение SMU отменено")
            self.write(f":OUTP{channel}:STAT ON")
            if int(self.query(f":OUTP{channel}:STAT?")) != 1:
                raise RuntimeError(f"SMU не подтвердил включение выхода {channel}")
        self.check_error()
        return self.state()

    def close(self):
        try:
            if self.device is not None:
                self.device.close()
        finally:
            self.manager.close()


def verified_off(driver):
    """A timed-out stream cannot confirm OFF; retry over a fresh identified link."""
    try:
        driver.all_off()
        return driver.state()
    except Exception:
        replacement = Smu200a(driver.host, driver.port, driver.log)
        try:
            replacement.all_off()
            return replacement.state()
        finally:
            replacement.close()
