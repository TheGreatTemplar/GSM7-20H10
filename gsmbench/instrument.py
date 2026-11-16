"""SCPI driver. Transport parameters must match the actual instrument LAN service."""

import socket
import time

from .core import parse_reading


class MeasurementCancelled(Exception):
    """The pending query was interrupted and the reply stream resynchronized."""


class LineTransport:
    """One outstanding query; cancellation writes do not consume its reply."""

    def query(self, command, timeout=3, stop=None, on_cancel=None):
        if self.pending_query is not None:
            raise ConnectionError(f"Не дочитан ответ на {self.pending_query}; переподключитесь к прибору")
        self.pending_query = command
        self.write(command)
        deadline = time.monotonic() + timeout
        cancelled = False
        while True:
            if not cancelled and stop is not None and stop.is_set():
                cancelled = True
                if on_cancel is not None:
                    on_cancel()
                # V1.24 may discard the pending query on ABOR. A distinct IDN
                # response is a barrier; numeric replies before it are stale.
                self.write("*IDN?")
                deadline = time.monotonic() + 3
            if b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                result = line.decode("ascii").strip()
                self.log("< " + result)
                if cancelled:
                    if any(model in result.upper() for model in ("GSM7-20H10", "GSM-20H10")):
                        self.pending_query = None
                        raise MeasurementCancelled()
                    continue
                self.pending_query = None
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Нет ответа на {command} за {timeout:g} с ({self.label})")
            if len(self.buffer) > 65536:
                raise ValueError("Ответ SCPI превышает 64 КиБ")
            data = self.read_chunk(min(remaining, 0.1))
            if data is not None:
                if not data:
                    raise ConnectionError("Прибор закрыл соединение")
                self.buffer += data


class SocketTransport(LineTransport):
    def __init__(self, host, port, log=lambda text: None):
        if not host.strip() or not 1 <= int(port) <= 65535:
            raise ValueError("Укажите IP и подтверждённый TCP-порт прибора")
        self.socket = socket.create_connection((host, int(port)), timeout=3)
        self.socket.settimeout(3)
        self.buffer = b""
        self.pending_query = None
        self.label = "TCP"
        self.log = log

    def write(self, command):
        self.log("> " + command)
        self.socket.settimeout(3)
        self.socket.sendall((command + "\n").encode("ascii"))

    def read_chunk(self, timeout):
        self.socket.settimeout(timeout)
        try:
            return self.socket.recv(4096)
        except TimeoutError:
            return None

    def close(self):
        self.socket.close()


class VisaTransport(LineTransport):
    def __init__(self, resource, log=lambda text: None):
        import pyvisa
        if not resource.upper().startswith("TCPIP"):
            raise ValueError("Для Ethernet укажите ресурс TCPIP…")
        self.log = log
        self.buffer = b""
        self.pending_query = None
        self.label = "VISA"
        self.manager = pyvisa.ResourceManager("@py")
        try:
            self.device = self.manager.open_resource(resource, open_timeout=3000)
            self.device.timeout = 3000
            self.device.write_termination = "\n"
            self.device.read_termination = "\n"
        except Exception:
            self.manager.close()
            raise

    def write(self, command):
        self.log("> " + command)
        self.device.timeout = 3000
        self.device.write(command)

    def read_chunk(self, timeout):
        import pyvisa
        self.device.timeout = max(1, int(timeout * 1000))
        try:
            # One byte per read preserves partial replies across short VISA timeouts.
            # read() can discard a partial line when a timeout is raised.
            return self.device.read_bytes(1, break_on_termchar=False)
        except pyvisa.errors.VisaIOError as error:
            if error.error_code == pyvisa.constants.StatusCode.error_timeout:
                return None
            raise

    def close(self):
        try:
            self.device.close()
        finally:
            self.manager.close()


class Instrument:
    def __init__(self, transport):
        self.io = transport

    def identify(self):
        identity = self.io.query("*IDN?")
        if not any(model in identity.upper() for model in ("GSM7-20H10", "GSM-20H10")):
            raise ValueError(f"Неожиданный прибор: {identity}")
        return identity

    def check_error(self):
        response = self.io.query(":SYST:ERR?")
        if int(response.split(",", 1)[0]) != 0:
            raise RuntimeError("Ошибка прибора: " + response)

    def output_off(self):
        self.io.write(":OUTP OFF")
        if self.io.query(":OUTP?").strip() not in ("0", "+0", "OFF"):
            raise RuntimeError("Прибор не подтвердил отключение выхода")

    def reset(self):
        self.output_off()
        self.io.write("*RST")
        # V1.24 returns 0 to *OPC? even when idle, and *WAI can block LAN.
        # Allow reset to settle, then require error and output-state readbacks.
        time.sleep(1.0)
        self.check_error()
        self.output_off()

    def prepare_list(self, recipe, stop):
        """Configure a complete native list and buffer without starting the output."""
        recipe.validate()
        self.reset()
        mode = "CURR" if recipe.kind == "diode" else "VOLT"
        sense = "VOLT" if recipe.kind == "diode" else "CURR"
        count = len(recipe.levels)
        terminal = "FRON" if recipe.terminals == "front" else "REAR"
        remote_sense = "ON" if recipe.wires == 4 else "OFF"
        commands = [f":ROUT:TERM {terminal}", f":SYST:RSEN {remote_sense}", f":SOUR:FUNC {mode}",
                    f":SOUR:{mode}:MODE FIX", f":SOUR:{mode}:RANG:AUTO ON",
                    f":SOUR:{mode} 0", f":SENS:{sense}:PROT {recipe.limit:.12g}",
                    ':SENS:FUNC "VOLT","CURR"', ":SENS:VOLT:RANG:AUTO ON",
                    ":SENS:CURR:RANG:AUTO ON", f":SENS:VOLT:NPLC {recipe.nplc:g}",
                    f":DISP:DIG {int(recipe.digits + 0.5)}", ":FORM:DATA ASC",
                    ":FORM:ELEM VOLT,CURR,STAT", f":TRIG:COUN {count}", ":ARM:COUN 1",
                    ":ARM:SOUR IMM", ":TRIG:SOUR IMM", ":TRIG:DEL 0",
                    ":SOUR:CLE:AUTO ON", ":SOUR:CLE:AUTO:MODE TCO",
                    ":SOUR:DEL:AUTO OFF", f":SOUR:DEL {recipe.delay:.12g}",
                    ":SOUR:SWE:RANG AUTO",
                    ":TRAC:FEED:CONT NEV", ":TRAC:CLE", f":TRAC:POIN {count}",
                    ":TRAC:FEED SENS"]
        for command in commands:
            if stop.is_set():
                return False
            self.io.write(command)
        self.check_error()
        actual_terminal = self.io.query(":ROUT:TERM?").strip().upper()
        if actual_terminal not in (("FRON", "FRONT") if recipe.terminals == "front" else ("REAR",)):
            raise RuntimeError("Прибор не подтвердил выбранные клеммы: " + actual_terminal)
        if float(self.io.query(":SYST:RSEN?")) != int(recipe.wires == 4):
            raise RuntimeError("Прибор не подтвердил схему измерения")
        if abs(float(self.io.query(":SENS:VOLT:NPLC?")) - recipe.nplc) > 1e-6:
            raise RuntimeError("Прибор не подтвердил NPLC")
        if float(self.io.query(":DISP:DIG?")) not in (recipe.digits, recipe.digits + 0.5):
            raise RuntimeError("Прибор не подтвердил разрядность")
        # The manual limits each LIST/APPEND message to 100 entries.
        for offset in range(0, count, 100):
            if stop.is_set():
                return False
            suffix = ":APP" if offset else ""
            values = ",".join(f"{level:.12g}" for level in recipe.levels[offset:offset + 100])
            self.io.write(f":SOUR:LIST:{mode}{suffix} {values}")
            self.check_error()
        self.io.write(f":SOUR:LIST:{mode}:STAR 1")
        self.io.write(f":SOUR:{mode}:MODE LIST")
        self.check_error()
        for query in (f":SOUR:LIST:{mode}:POIN?", ":TRAC:POIN?", ":TRIG:COUN?"):
            if float(self.io.query(query)) != count:
                raise RuntimeError(f"Прибор не подтвердил число точек ({count}): {query}")
        auto_mode = self.io.query(":SOUR:CLE:AUTO:MODE?").strip().upper()
        if auto_mode not in ("TCO", "TCOUNT"):
            raise RuntimeError(f"Прибор не подтвердил отключение в конце серии: {auto_mode}")
        if self.io.query(":SOUR:SWE:RANG?").strip().upper() != "AUTO":
            raise RuntimeError("Прибор не подтвердил автоматический выбор диапазона серии")
        if float(self.io.query(":TRAC:POIN:ACT?")) != 0:
            raise RuntimeError("Буфер измерений не очищен")
        self.check_error()
        return not stop.is_set()

    def read_buffer(self, recipe, on_point):
        # V1.24 TRAC:DATA? returns U,I,R regardless of FORM:ELEM; FETC? honors it.
        fields = self.io.query(":FETC?").strip().split(",")
        if len(fields) != len(recipe.levels) * 3:
            raise ValueError(f"Размер буфера: получено {len(fields)} полей, ожидалось {len(recipe.levels) * 3}")
        points = []
        for index, level in enumerate(recipe.levels):
            point = parse_reading(",".join(fields[index * 3:index * 3 + 3]), level)
            if recipe.kind == "diode" and point.voltage < 0:
                raise ValueError(f"Точка {index + 1}: отрицательное Vf ({point.voltage:g} В) "
                                 f"при прямом токе {level * 1e6:g} мкА. "
                                 "Проверьте подключение и установление режима на малых токах.")
            points.append(point)
        for point in points:
            on_point(point)

    def wait_series(self, recipe, stop):
        # On V1.24 a query during INIT blocks even ABOR processing. Leave the
        # command channel free during acquisition; Event.wait wakes on STOP.
        # Include time for two measurements and source autorange settling.
        return stop.wait(len(recipe.levels) * (recipe.delay + 0.21 + 0.04 * recipe.nplc) + 0.5)

    def measure(self, recipe, stop, on_point):
        recipe.validate()
        failure = None
        try:
            if not self.prepare_list(recipe, stop):
                return
            self.io.write(":TRAC:FEED:CONT NEXT")
            self.check_error()
            if stop.is_set():
                return
            self.io.write(":INIT")
            count = len(recipe.levels)
            deadline = time.monotonic() + max(10, count * (recipe.delay + 1 + 0.04 * recipe.nplc) + 5)
            previous = 0
            cancel_sent = False

            def cancel_pending():
                nonlocal cancel_sent
                if cancel_sent:
                    return
                cancel_sent = True
                self.io.write(":ABOR")
                self.io.write(":OUTP OFF")

            if self.wait_series(recipe, stop):
                cancel_pending()
                return
            while True:
                if stop.is_set():
                    self.io.write(":ABOR")
                    return
                actual_value = float(self.io.query(":TRAC:POIN:ACT?",
                                                   timeout=max(3, deadline - time.monotonic()),
                                                   stop=stop, on_cancel=cancel_pending))
                if stop.is_set():
                    cancel_pending()
                    return
                if not actual_value.is_integer() or not previous <= actual_value <= count:
                    raise RuntimeError(f"Некорректное число точек в буфере: {actual_value}")
                actual = int(actual_value)
                previous = actual
                if actual == count:
                    break
                self.check_error()
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Серия не завершилась: {actual}/{count} точек")
                stop.wait(0.1)
            # The full buffer is read once, after source output is disabled.
            self.output_off()
            self.read_buffer(recipe, on_point)
            self.check_error()
        except MeasurementCancelled:
            return
        except Exception as error:
            failure = error
            try:
                self.io.write(":ABOR")
            except Exception:
                pass
            raise
        finally:
            try:
                self.output_off()
            except Exception as shutdown_error:
                if failure is not None:
                    raise RuntimeError(f"{failure}\nОтключение выхода не подтверждено: {shutdown_error}. "
                                       "Проверьте OUTPUT на приборе.") from failure
                raise RuntimeError(f"Отключение выхода не подтверждено: {shutdown_error}. "
                                   "Проверьте OUTPUT на приборе.") from shutdown_error
