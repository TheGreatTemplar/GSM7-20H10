"""MNIPI E7-29 binary USB/COM protocol, manual section 9.6.13, pages 20–22."""

from datetime import datetime, timezone
import math
import struct
import threading
import time

import serial


PARAMETERS = {
    'z_ohm': ('|Z|', 'Ом'), 'phase_deg': ('φ', '°'),
    'rs_ohm': ('Rs', 'Ом'), 'rp_ohm': ('Rp', 'Ом'), 'xs_ohm': ('Xs', 'Ом'),
    'ls_h': ('Ls', 'Гн'), 'lp_h': ('Lp', 'Гн'), 'cs_f': ('Cs', 'Ф'), 'cp_f': ('Cp', 'Ф'),
    'q': ('Q', ''), 'd': ('D', ''), 'g_s': ('G', 'См'), 'b_s': ('B', 'См'),
}


def impedance_parameters(frequency_hz, z_ohm, phase_rad):
    if (not all(math.isfinite(value) for value in (frequency_hz, z_ohm, phase_rad))
            or frequency_hz <= 0 or z_ohm < 0 or abs(phase_rad) > math.pi + 1e-5):
        raise ValueError('Недостоверные частота, |Z| или фаза Е7-29')
    omega = 2 * math.pi * frequency_hz
    resistance = z_ohm * math.cos(phase_rad)
    reactance = z_ohm * math.sin(phase_rad)
    conductance = resistance / z_ohm ** 2 if z_ohm else None
    susceptance = -reactance / z_ohm ** 2 if z_ohm else None
    return dict(z_ohm=z_ohm, phase_rad=phase_rad, phase_deg=math.degrees(phase_rad),
        rs_ohm=resistance, xs_ohm=reactance, g_s=conductance, b_s=susceptance,
        rp_ohm=1/conductance if conductance else None,
        ls_h=reactance/omega if reactance > 0 else None,
        lp_h=-1/(omega*susceptance) if susceptance is not None and susceptance < 0 else None,
        cs_f=-1/(omega*reactance) if reactance < 0 else None,
        cp_f=susceptance/omega if susceptance is not None and susceptance > 0 else None,
        q=abs(reactance)/resistance if resistance > 0 else None,
        d=resistance/abs(reactance) if resistance >= 0 and reactance else None)


def decode_measurement(packet):
    if len(packet) != 20 or packet[:2] != bytes((0xAA, 72)):
        raise ValueError('Е7-29: неверная длина или заголовок измерительного пакета')
    flags, mode, speed, measurement_range = packet[2:6]
    if not flags & 0x80 or mode > 3 or speed > 2 or measurement_range > 6:
        raise ValueError('Е7-29: цикл не завершён или неверные параметры пакета')
    bias, frequency, impedance, phase = struct.unpack('>hIff', packet[6:])
    if not 50000 <= frequency <= 15000000:
        raise ValueError('Е7-29: частота ответа вне диапазона 50 кГц…15 МГц')
    if not 0 <= bias <= 400:
        raise ValueError('Е7-29: недостоверное смещение в ответе')
    overloaded = bool(flags & 4)
    values = {} if overloaded else impedance_parameters(frequency, impedance, phase)
    return dict(measured_at=datetime.now(timezone.utc).isoformat(), frequency_hz=frequency,
        bias_v=bias/10, flags=flags, mode=mode, speed=speed, range_index=measurement_range,
        autorange=bool(flags & 1), auto_parameter=bool(flags & 8),
        equivalent_circuit='parallel' if flags & 16 else 'series', overloaded=overloaded,
        status='overload' if overloaded else 'measured', values=values, raw_hex=packet.hex())


class E729:
    def __init__(self, port='COM9', log=lambda message: None, serial_factory=None, timeout=3):
        self.port, self.log, self.timeout = port, log, timeout
        self.device = None
        self.broken = False
        self.lock = threading.RLock()
        try:
            self.device = (serial_factory or serial.Serial)()
            self.device.port = port
            self.device.baudrate = 9600
            self.device.bytesize, self.device.parity, self.device.stopbits = 8, 'N', 1
            self.device.rtscts = self.device.dsrdtr = self.device.xonxoff = False
            self.device.dtr = self.device.rts = False
            # Changing timeout on an open pySerial/Windows port also calls
            # SetCommState. CP210x can corrupt a frame if reconfigured mid-reply.
            self.device.timeout = min(0.2, timeout)
            self.device.write_timeout = timeout
            self.device.open()
            self._begin(64)
            name = self._read_exact(4)
            # Firmware V1.1 appends a C-string terminator; other revisions may omit it.
            terminator = self.device.read(1)
            if name not in (b'E729', bytes((0xC5, 0x37, 0x32, 0x39))) or terminator not in (b'', b'\0'):
                raise ValueError('На порту не подтверждён Е7-29: ' + repr(name + terminator))
            self.identity = 'МНИПИ Е7-29 · ' + name.decode('cp1251')
            self.log('Е7-29 < ' + self.identity)
        except Exception:
            self.close()
            raise

    def _read_exact(self, size):
        packet = bytearray()
        deadline = time.monotonic() + self.timeout
        while len(packet) < size:
            if time.monotonic() >= deadline:
                self.broken = True
                raise TimeoutError(f'Е7-29 {self.port}: неполный ответ ({len(packet)}/{size} байт); требуется переподключение')
            try:
                packet.extend(self.device.read(size-len(packet)))
            except Exception:
                self.broken = True
                raise
        return bytes(packet)

    def _begin(self, command, payload=b'', optional_ack=False):
        if self.broken:
            raise ConnectionError('Е7-29: после ошибки обмена требуется переподключение')
        message = bytes((0xAA, command)) + payload
        self.log('Е7-29 > ' + message.hex(' '))
        try:
            if self.device.write(message) != len(message):
                raise IOError('Е7-29: неполная отправка команды')
            if optional_ack:
                first = self.device.read(1)
                if not first:
                    self.log('Е7-29: ACK установки отсутствует; проверяем фактическое состояние')
                    return False
                response = first + self._read_exact(1)
            else:
                response = self._read_exact(2)
            if response != bytes((0xAA, command)):
                raise ValueError('Е7-29: неожиданный заголовок ' + response.hex(' '))
            return True
        except Exception:
            self.broken = True
            raise

    def command(self, number, payload=b''):
        with self.lock:
            acknowledged = self._begin(number, payload, optional_ack=True)
            if acknowledged:
                self.log('Е7-29 < ACK ' + str(number))
            return acknowledged

    def _verify_setting(self, predicate, expected_frequency=None):
        time.sleep(1)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            point = self.read_measurement(expected_frequency=expected_frequency, timeout=5)
            if predicate(point):
                return
        raise ValueError('Е7-29: установка не подтверждена чтением фактического состояния')

    def set_frequency(self, frequency_hz):
        if not math.isfinite(frequency_hz) or not 50000 <= frequency_hz <= 15000000 or frequency_hz != int(frequency_hz):
            raise ValueError('Е7-29: частота 50 кГц…15 МГц, целое число герц')
        if not self.command(67, struct.pack('>I', int(frequency_hz))):
            self._verify_setting(lambda point: point['frequency_hz'] == frequency_hz, int(frequency_hz))

    def set_range(self, measurement_range):
        if measurement_range == -1:
            acknowledged = self.command(65)
        elif type(measurement_range) is int and 0 <= measurement_range <= 6:
            acknowledged = self.command(66, bytes((measurement_range,)))
        else:
            raise ValueError('Е7-29: неверный диапазон')
        if not acknowledged:
            self._verify_setting(lambda point: point['autorange'] if measurement_range == -1 else
                not point['autorange'] and point['range_index'] == measurement_range)

    def set_speed(self, speed):
        if speed not in (0, 1):
            raise ValueError('Через интерфейс доступны скорости «Быстро» и «Норма»')
        if not self.command(73 if speed == 0 else 74):
            self._verify_setting(lambda point: point['speed'] == speed)

    def read_measurement(self, stop=None, expected_frequency=None, timeout=15):
        stop = stop or threading.Event()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if stop.is_set():
                raise InterruptedError('Измерение Е7-29 остановлено')
            with self.lock:
                self._begin(72)
                flags = self._read_exact(1)
                if flags == b'\0':
                    packet = None
                else:
                    packet = bytes((0xAA,72)) + flags + self._read_exact(17)
                    self.log('Е7-29 < ' + packet.hex(' '))
            if packet:
                point = decode_measurement(packet)
                if expected_frequency is None or point['frequency_hz'] == expected_frequency:
                    return point
            stop.wait(0.25)
        raise TimeoutError('Е7-29: нет завершённого измерения на заданной частоте. Проверьте запуск и отключите частотный анализ на панели прибора.')

    def close(self):
        if self.device is not None:
            self.device.close()
