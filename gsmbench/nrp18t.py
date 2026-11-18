"""Native USBTMC SCPI for NRP18T using the installed system VISA library."""

import math
import time

from .rsa import AcquisitionCancelled


class PowerReadingError(ValueError):
    """The reply was complete, but it is not a usable power measurement."""


class Nrp18t:
    def __init__(self, resource, log=lambda line: None, manager_factory=None):
        if manager_factory is None:
            import pyvisa
            manager_factory = pyvisa.ResourceManager
        self.log = log
        self.manager = manager_factory()
        self.device = None
        try:
            self.device = self.manager.open_resource(resource, open_timeout=3000)
            self.device.timeout = 3000
            self.device.write_termination = "\n"
            self.device.read_termination = "\n"
            self.identity = self.query("*IDN?")
            if len(self.identity.split(",")) < 2 or self.identity.split(",")[1].strip().upper() != "NRP18T":
                raise ValueError("Ожидался NRP18T: " + self.identity)
        except Exception:
            self.close()
            raise

    def write(self, command):
        self.log("NRP18T > " + command)
        self.device.write(command)

    def query(self, command):
        self.log("NRP18T > " + command)
        result = self.device.query(command).strip()
        self.log("NRP18T < " + result)
        return result

    def check_error(self):
        result = self.query("SYST:ERR?")
        if int(result.split(",", 1)[0]):
            raise ValueError("NRP18T: " + result)

    def complete(self, stop, timeout=60):
        self.write("*OPC")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if stop.is_set():
                self.write("ABOR")
                raise AcquisitionCancelled()
            status = int(self.query("*ESR?"))
            if status & 0x3c:
                raise ValueError(f"NRP18T: ошибка операции, ESR={status}")
            if status & 1:
                self.check_error()
                return
            stop.wait(0.1)
        raise TimeoutError("NRP18T не завершил измерение / установку нуля")

    def prepare(self, stop, averages=16):
        # Zero only after the caller has verified that both SMU outputs are OFF.
        self.write("*RST")
        self.complete(stop)
        self.query("*ESR?")
        self.write("CAL:ZERO:AUTO ONCE")
        # Zeroing is synchronous: the manual forbids queries while it runs.
        # All RF outputs are already verified OFF, so cancellation here is safe.
        if stop.wait(10):
            raise AcquisitionCancelled()
        previous_timeout = self.device.timeout
        try:
            self.device.timeout = 30000
            zero_error = self.query("SYST:SERR?")
            if int(zero_error.split(",", 1)[0]):
                raise ValueError("NRP18T: установка нуля не подтверждена: " + zero_error)
        finally:
            self.device.timeout = previous_timeout
        for command in ('SENS:FUNC "POW:AVG"', "INIT:CONT OFF", "SENS:BUFF:STAT OFF",
                "TRIG:SOUR IMM", "TRIG:COUN 1", "UNIT:POW W", "FORM ASC",
                "SENS:AVER:COUN:AUTO OFF", f"SENS:AVER:COUN {averages}", "SENS:AVER:TCON REP"):
            self.write(command)
        self.check_error()
        actual = dict(function=self.query("SENS:FUNC?").strip('"'), unit=self.query("UNIT:POW?"),
            continuous=int(self.query("INIT:CONT?")), averages=int(self.query("SENS:AVER:COUN?")),
            buffer=int(self.query("SENS:BUFF:STAT?")), zero_confirmed=True)
        if (actual["function"] != "POW:AVG" or actual["unit"] != "W" or actual["continuous"]
                or actual["buffer"] or actual["averages"] != averages):
            raise ValueError("NRP18T не подтвердил режим средней мощности / единицы / усреднение")
        return actual

    def start_level(self, frequency_hz, stop):
        """Drop the old averaging history and one reading after an RF transition."""
        self.write("SENS:AVER:RES")
        self.check_error()
        try:
            discarded = self.read_dbm(frequency_hz, stop)
            self.log(f"NRP18T: первый отсчёт после переключения исключён: {discarded:+.4f} dBm")
            return dict(discarded_dbm=discarded)
        except PowerReadingError as error:
            self.log("NRP18T: переходный отсчёт исключён: " + str(error))
            return dict(discarded_error=str(error))

    def read_dbm(self, frequency_hz, stop):
        if stop.is_set():
            raise AcquisitionCancelled()
        self.write(f"SENS:FREQ {frequency_hz:.12g}")
        self.query("*ESR?")
        self.write("INIT:IMM")
        self.complete(stop)
        watts = float(self.query("FETC?"))
        self.check_error()
        if (self.query("UNIT:POW?") != "W" or not math.isclose(float(self.query("SENS:FREQ?")), frequency_hz, abs_tol=0.1, rel_tol=0)
                or int(self.query("STAT:QUES:COND?"))):
            raise ValueError("NRP18T: недостоверный статус либо изменились единицы / частота коррекции")
        if not math.isfinite(watts) or not 10 ** (-65 / 10) <= watts <= 0.1:
            level = f"{10 * math.log10(watts) + 30:+.3f} dBm" if math.isfinite(watts) and watts > 0 else f"{watts:g} Вт"
            raise PowerReadingError(f"NRP18T: {level}, результат вне рабочего диапазона −35…+20 dBm")
        return 10 * math.log10(watts) + 30

    def close(self):
        try:
            if self.device is not None:
                self.device.close()
                self.device = None
        finally:
            self.manager.close()
