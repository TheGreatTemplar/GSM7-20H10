"""Keithley DMM6500 native SCPI over the documented TCP socket (5025)."""

import math
import socket
import time

from .rsa import AcquisitionCancelled


class Dmm6500:
    def __init__(self, host="172.16.236.45", port=5025, log=lambda line: None, socket_factory=None):
        self.log, self.host, self.port = log, host, int(port)
        self.device = None
        self.broken = False
        self.configured = False
        try:
            self.device = (socket_factory or socket.create_connection)((host, self.port), timeout=3)
            self.device.settimeout(0.1)
            self.identity = self.query("*IDN?")
            if (len(self.identity.split(",")) < 2
                    or self.identity.split(",")[1].strip().upper() not in ("DMM6500", "MODEL DMM6500")):
                raise ValueError("Ожидался DMM6500 в режиме SCPI: " + self.identity)
            for _ in range(21):
                error = self.query(":SYST:ERR?")
                if int(error.split(",", 1)[0]) == 0:
                    break
                self.log("DMM6500: прежняя ошибка " + error)
            else:
                raise ValueError("Очередь ошибок DMM6500 не опустела")
        except Exception:
            self.close()
            raise

    def write(self, command):
        if self.broken:
            raise ConnectionError("После ошибки обмена DMM6500 требуется новое соединение")
        self.log("DMM6500 > " + command)
        self.device.sendall((command + "\n").encode("ascii"))

    def query(self, command, stop=None):
        if stop is not None and stop.is_set():
            raise AcquisitionCancelled()
        try:
            self.write(command)
            response = bytearray()
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if stop is not None and stop.is_set():
                    raise AcquisitionCancelled()
                try:
                    chunk = self.device.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    raise ConnectionError("DMM6500 закрыл соединение")
                response.extend(chunk)
                if b"\n" in response:
                    line, remainder = bytes(response).split(b"\n", 1)
                    if remainder.strip():
                        raise ValueError("Лишний ответ DMM6500; нарушен порядок запросов")
                    result = line.decode("ascii").strip()
                    self.log("DMM6500 < " + result)
                    return result
                if len(response) > 4096:
                    raise ValueError("Слишком длинный ответ DMM6500")
            raise TimeoutError("Нет ответа DMM6500 на " + command + " за 15 с; проверьте режим SCPI и IP/порт")
        except Exception:
            self.broken = True
            raise

    def check_error(self):
        error = self.query(":SYST:ERR?")
        if int(error.split(",", 1)[0]):
            raise ValueError("DMM6500: " + error)

    def configure_dc(self, nplc=10, voltage_range=10, high_impedance=True, digits=6.5):
        if nplc not in (0.02, 0.2, 1, 10) or voltage_range not in (0, 0.1, 1, 10, 100, 1000):
            raise ValueError("Неподдерживаемый диапазон / NPLC DMM6500; 100 NPLC недоступно")
        if digits not in (4.5, 5.5, 6.5):
            raise ValueError("Разрядность DMM6500: 4,5; 5,5 или 6,5")
        previous = self.query(":SENS:FUNC?")
        for command in (':SENS:FUNC "VOLT:DC"', ":ABOR", ":FORM:DATA ASC", ":FORM:ASC:PREC 9",
                ":SENS:VOLT:RANG:AUTO " + ("ON" if voltage_range == 0 else "OFF")):
            self.write(command)
        if voltage_range:
            self.write(f":SENS:VOLT:RANG {voltage_range:g}")
        for command in (f":SENS:VOLT:NPLC {nplc:g}", f":DISP:VOLT:DIG {int(digits)}",
                ":SENS:VOLT:INP " + ("AUTO" if high_impedance else "MOHM10"),
                ":SENS:VOLT:AZER ON", ":SENS:VOLT:AVER OFF", ":SENS:COUN 1"):
            self.write(command)
        self.check_error()
        function = self.query(":SENS:FUNC?").strip('"').upper()
        actual_nplc = float(self.query(":SENS:VOLT:NPLC?"))
        actual_digits = int(float(self.query(":DISP:VOLT:DIG?"))) + 0.5
        impedance = self.query(":SENS:VOLT:INP?").upper()
        auto_range = int(self.query(":SENS:VOLT:RANG:AUTO?"))
        actual_range = float(self.query(":SENS:VOLT:RANG?"))
        if (function not in ("VOLT", "VOLT:DC") or not math.isclose(actual_nplc, nplc, abs_tol=1e-6)
                or actual_digits != digits or impedance != ("AUTO" if high_impedance else "MOHM10")
                or bool(auto_range) != (voltage_range == 0)
                or (voltage_range and not math.isclose(actual_range, voltage_range, rel_tol=1e-6))):
            raise ValueError("DMM6500 не подтвердил настройки DC V / NPLC / разрядности / входного сопротивления")
        self.configured = True
        return dict(identity=self.identity, host=self.host, port=self.port, function=function, nplc=actual_nplc,
            range_v=voltage_range, actual_range_v=actual_range, high_impedance=high_impedance,
            requested_digits=digits, display_digits=actual_digits, previous_configuration=previous,
            digits_basis="front_panel_display_only; remote_readings_keep_full_precision")

    def read_voltage(self, stop):
        if not self.configured:
            raise ValueError("Сначала настройте DMM6500 на DC V")
        value = float(self.query(":READ?", stop))
        if not math.isfinite(value) or abs(value) >= 1e9:
            raise ValueError("DMM6500: перегрузка или недостоверное напряжение")
        self.check_error()
        return value

    def close(self):
        if self.device is not None:
            self.device.close()
            self.device = None
