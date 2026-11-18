"""34401A serial DC-voltage acquisition with explicit DTR/DSR recovery."""

import math
import time

from .rsa import AcquisitionCancelled


class Hp34401a:
    def __init__(self, port="COM3", baud=9600, log=lambda line: None, serial_factory=None):
        if serial_factory is None:
            import serial
            serial_factory = serial.Serial
        self.log = log
        self.port, self.baud = port, baud
        self.device = None
        self.broken = False
        self.configured = False
        try:
            self.device = serial_factory(port=port, baudrate=baud, bytesize=8, parity="N", stopbits=2,
                timeout=0.05, write_timeout=2, rtscts=False, dsrdtr=False, xonxoff=False)
            self.device.rts = True
            self.recover()
            self.write("SYST:REM")
            self.identity = self.query("*IDN?")
            fields = self.identity.split(",")
            if len(fields) < 2 or fields[1].strip() != "34401A":
                raise ValueError("Ожидался 34401A: " + self.identity)
            for index in range(21):
                error = self.query("SYST:ERR?")
                if int(error.split(",", 1)[0]) == 0:
                    break
                self.log("34401A: прежняя ошибка " + error)
            else:
                raise ValueError("Очередь ошибок 34401A не опустела")
        except Exception:
            self.close()
            raise

    def recover(self):
        self.log("34401A: DTR low -> Ctrl-C -> DTR high; очистка интерфейса без *RST")
        self.device.dtr = False
        time.sleep(0.2)
        self.device.write(b"\x03")
        self.device.flush()
        time.sleep(0.2)
        self.device.dtr = True
        time.sleep(0.3)
        self.device.reset_input_buffer()
        self.broken = False

    def write(self, command):
        if self.broken:
            raise ConnectionError("После ошибки обмена 34401A требуется новое соединение")
        deadline = time.monotonic() + 3
        while not self.device.dsr:
            if time.monotonic() >= deadline:
                self.broken = True
                raise TimeoutError("34401A: DSR не готов, команда не отправлена")
            time.sleep(0.02)
        self.log("34401A > " + command)
        try:
            self.device.write((command + "\n").encode("ascii"))
            self.device.flush()
        except Exception:
            self.broken = True
            raise

    def query(self, command, stop=None):
        if stop is not None and stop.is_set():
            raise AcquisitionCancelled()
        self.write(command)
        reply = bytearray()
        deadline = time.monotonic() + 10
        try:
            while time.monotonic() < deadline:
                if stop is not None and stop.is_set():
                    raise AcquisitionCancelled()
                reply.extend(self.device.read(max(1, self.device.in_waiting)))
                if b"\n" in reply:
                    line, remainder = bytes(reply).split(b"\n", 1)
                    if remainder.strip():
                        raise ValueError("34401A вернул лишний ответ; нарушен порядок запросов")
                    text = line.decode("ascii").strip()
                    self.log("34401A < " + text)
                    return text
                if len(reply) > 4096:
                    raise ValueError("Слишком длинный ответ 34401A")
            raise TimeoutError("Нет полного ответа 34401A на " + command)
        except Exception:
            self.broken = True
            raise

    def check_error(self):
        error = self.query("SYST:ERR?")
        if int(error.split(",", 1)[0]):
            raise ValueError("34401A: " + error)

    def configure_dc(self, nplc=10, voltage_range=10, high_impedance=True, digits=None):
        if nplc not in (0.02, 0.2, 1, 10, 100) or voltage_range not in (0, 0.1, 1, 10, 100, 1000):
            raise ValueError("Неподдерживаемый NPLC или диапазон DC V 34401A")
        previous = self.query("CONF?")
        value = "AUTO" if voltage_range == 0 else f"{voltage_range:g}"
        resolution = None
        if digits is not None:
            if digits not in (4.5, 5.5, 6.5) or voltage_range == 0:
                raise ValueError("Для заданной разрядности выберите фиксированный диапазон DC V")
            resolution = voltage_range * {4.5: 1e-4, 5.5: 1e-5, 6.5: 1e-6}[digits]
            value += f",{resolution:.12g}"
        for command in ("CONF:VOLT:DC " + value, f"VOLT:DC:NPLC {nplc:g}", "ZERO:AUTO ON",
                "INP:IMP:AUTO " + ("ON" if high_impedance else "OFF"), "CALC:STAT OFF",
                "TRIG:SOUR IMM", "TRIG:COUN 1", "SAMP:COUN 1"):
            self.write(command)
        self.check_error()
        function = self.query("FUNC?").strip('"')
        actual_nplc = float(self.query("VOLT:DC:NPLC?"))
        actual_resolution = float(self.query("VOLT:DC:RES?")) if digits is not None else None
        if function not in ("VOLT", "VOLT:DC") or not math.isclose(actual_nplc, nplc, abs_tol=1e-6):
            raise ValueError("34401A не подтвердил DC V / NPLC")
        self.configured = True
        return dict(identity=self.identity, port=self.port, baud=self.baud, frame="8N2", function=function,
            nplc=actual_nplc, range_v=voltage_range, high_impedance=high_impedance, previous_configuration=previous,
            requested_digits=digits, requested_resolution_v=resolution, actual_resolution_v=actual_resolution)

    def read_voltage(self, stop):
        if not self.configured:
            raise ValueError("Сначала настройте 34401A на DC V")
        value = float(self.query("READ?", stop))
        if not math.isfinite(value) or abs(value) >= 1e9:
            raise ValueError("34401A: перегрузка или недостоверное напряжение")
        self.check_error()
        return value

    def close(self):
        if self.device is None:
            return
        try:
            if self.broken:
                self.recover()
            self.write("SYST:LOC")
            time.sleep(0.1)
        finally:
            self.device.close()
            self.device = None
