"""Verified AKIP-3208 / SSG3000X socket command dialect (including OEM IDN)."""

import math

from .rf_instruments import socket_resource


class Ssg3000x:
    def __init__(self, host, port=5025, log=lambda line: None, manager_factory=None):
        if manager_factory is None:
            import pyvisa
            manager_factory = pyvisa.ResourceManager
        self.host, self.port, self.log = host, port, log
        self.device = None
        self.broken = False
        self.prepared = False
        self.manager = manager_factory("@py")
        try:
            self.device = self.manager.open_resource(socket_resource(host, port), open_timeout=3000)
            self.device.timeout = 3000
            self.device.read_termination = self.device.write_termination = "\n"
            self.identity = self.query("*IDN?")
            fields = self.identity.split(",")
            supported = len(fields) >= 3 and ("SSG3" in fields[1].upper() or "SSG3X" in fields[2].upper() or "АКИП-3208" in fields[1].upper())
            if not supported:
                raise ValueError("Ожидался АКИП-3208 / SSG3000X: " + self.identity)
        except Exception:
            self.close()
            raise

    def write(self, command):
        self.log("AKIP > " + command)
        self.device.write(command)

    def query(self, command):
        if self.broken:
            raise ConnectionError("После ошибки чтения АКИП требуется новое соединение")
        self.log("AKIP > " + command)
        try:
            response = self.device.query(command).strip()
        except Exception:
            self.broken = True
            raise
        self.log("AKIP < " + response)
        return response

    def state(self):
        return dict(identity=self.identity, output=int(self.query(":OUTP?")),
            frequency_hz=float(self.query(":FREQ?")), level_dbm=float(self.query(":POW?")),
            modulation=int(self.query(":MOD?")), sweep=self.query(":SWE:STAT?").upper(),
            correction=int(self.query(":CORR:FLAT?")), power_offset_db=float(self.query(":POW:OFFS?")))

    def check_error(self):
        error = self.query(":SYST:ERR?")
        if int(error.split(",", 1)[0]):
            raise ValueError("АКИП: " + error)

    def off(self):
        self.write(":OUTP OFF")
        if int(self.query(":OUTP?")) != 0:
            raise ValueError("АКИП не подтвердил RF OFF")

    def prepare(self, frequency, level):
        self.off()
        self.check_error()
        for command in (":SWE:STAT OFF", ":MOD OFF", ":CORR:FLAT OFF", ":POW:OFFS 0"):
            self.write(command)
        self.prepared = True
        return self.setpoint(frequency, level)

    def setpoint(self, frequency, level):
        if not self.prepared:
            raise ValueError("Сначала подготовьте CW АКИП при RF OFF")
        if not math.isfinite(frequency) or not 9000 <= frequency <= 3.2e9 or not math.isfinite(level) or not -110 <= level <= 20:
            raise ValueError("АКИП: частота 9 кГц…3,2 ГГц, уровень −110…+20 dBm; задание подтверждается прибором")
        self.write(f":FREQ {frequency:.12g} Hz")
        self.write(f":POW {level:.12g} dBm")
        self.check_error()
        state = self.state()
        if (not math.isclose(state["frequency_hz"], frequency, abs_tol=0.05, rel_tol=0) or
                not math.isclose(state["level_dbm"], level, abs_tol=0.011, rel_tol=0) or
                state["modulation"] or state["sweep"] != "OFF" or state["correction"] or state["power_offset_db"]):
            raise ValueError("АКИП не подтвердил частоту, уровень или чистый CW")
        return state

    def enable(self, stop):
        if not self.prepared or stop.is_set():
            raise InterruptedError("АКИП не подготовлен либо включение отменено")
        self.write(":OUTP ON")
        if int(self.query(":OUTP?")) != 1:
            raise ValueError("АКИП не подтвердил RF ON")
        self.check_error()
        return self.state()

    def close(self):
        try:
            if self.device is not None:
                self.device.close()
        finally:
            self.manager.close()


def verified_ssg_off(driver):
    try:
        driver.off()
        return driver.state()
    except Exception:
        replacement = Ssg3000x(driver.host, driver.port, driver.log)
        try:
            replacement.off()
            return replacement.state()
        finally:
            replacement.close()
