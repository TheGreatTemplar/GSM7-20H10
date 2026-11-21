"""R&S SMR20 serial CW source: 19200 baud, 8N2, RTS/CTS."""

import math
import threading

import serial


class Smr20:
    def __init__(self, port='COM3', baud=19200, log=lambda line: None, serial_factory=None):
        self.port, self.baud, self.log = port, int(baud), log
        self.prepared = False
        self.broken = False
        self.device = None
        self.lock = threading.RLock()
        try:
            self.device = (serial_factory or serial.Serial)(port, baudrate=self.baud,
                bytesize=8, parity='N', stopbits=2, rtscts=True, dsrdtr=False,
                timeout=3, write_timeout=3)
            self.identity = self.query('*IDN?')
            if len(self.identity.split(',')) < 3 or self.identity.split(',')[1].strip().upper() != 'SMR20':
                raise ValueError('Ожидался R&S SMR20: ' + self.identity)
        except Exception:
            self.close()
            raise

    def write(self, command):
        self.log('SMR > ' + command)
        with self.lock:
            self.device.write((command + '\n').encode('ascii'))

    def query(self, command):
        with self.lock:
            if self.broken:
                raise ConnectionError('После ошибки чтения SMR20 требуется новое соединение')
            try:
                self.write(command)
                response = self.device.read_until(b'\n')
                if not response.endswith(b'\n'):
                    raise TimeoutError('SMR20: нет полного ответа на ' + command)
                value = response.decode('ascii').strip()
                self.log('SMR < ' + value)
                return value
            except Exception:
                self.broken = True
                raise

    def state(self):
        return dict(identity=self.identity, output=int(self.query(':OUTP?')),
            frequency_hz=float(self.query(':FREQ?')), level_dbm=float(self.query(':POW?')),
            frequency_mode=self.query(':FREQ:MODE?'), power_mode=self.query(':POW:MODE?'),
            am=int(self.query(':AM:STAT?')), fm=int(self.query(':FM:STAT?')),
            pulse=int(self.query(':PULM:STAT?')), alc=int(self.query(':POW:ALC:STAT?')),
            attenuator_mode=self.query(':OUTP:AMOD?').upper(),
            correction=int(self.query(':CORR:STAT?')), power_offset_db=float(self.query(':POW:OFFS?')))

    def check_error(self):
        error = self.query(':SYST:ERR?')
        code = int(error.split(',', 1)[0])
        if code == 171:
            self.log('SMR20: опорный генератор ещё прогревается (Oven cold); точность частоты не установилась')
        elif code:
            raise ValueError('SMR20: ' + error)

    def off(self):
        self.write(':OUTP OFF')
        if int(self.query(':OUTP?')) != 0:
            raise RuntimeError('SMR20 не подтвердил RF OFF')

    def prepare(self, frequency, level):
        self.off()
        self.check_error()
        for command in (':FREQ:MODE FIX', ':POW:MODE FIX', ':POW:UNIT DBM', ':OUTP:AMOD AUTO', ':AM:STAT OFF', ':FM:STAT OFF',
                ':PULM:STAT OFF', ':CORR:STAT OFF', ':POW:OFFS 0', ':POW:ALC:STAT ON'):
            self.write(command)
        self.bounds = dict(fmin=float(self.query(':FREQ? MIN')), fmax=float(self.query(':FREQ? MAX')),
            pmin=float(self.query(':POW? MIN')),
            pmax=min(float(self.query(':POW? MAX')), float(self.query(':POW:LIM?'))))
        self.check_error()
        self.prepared = True
        return self.setpoint(frequency, level)

    def setpoint(self, frequency, level):
        if not self.prepared:
            raise RuntimeError('Сначала подготовьте CW SMR20 при RF OFF')
        if (not all(math.isfinite(value) for value in (frequency, level))
                or not self.bounds['fmin'] <= frequency <= self.bounds['fmax']
                or not self.bounds['pmin'] <= level <= self.bounds['pmax']):
            raise ValueError('SMR20: задание вне пределов прибора: ' + str(self.bounds))
        self.write(f':FREQ {frequency:.12g} Hz')
        self.write(f':POW {level:.12g} dBm')
        self.check_error()
        state = self.state()
        if (not math.isclose(state['frequency_hz'], frequency, abs_tol=0.05, rel_tol=0)
                or not math.isclose(state['level_dbm'], level, abs_tol=0.011, rel_tol=0)
                or any(state[key] for key in ('am', 'fm', 'pulse', 'correction', 'power_offset_db'))
                or not state['alc'] or state['frequency_mode'] not in ('CW', 'FIX')
                or state['power_mode'] not in ('CW', 'FIX') or state['attenuator_mode'] != 'AUTO'):
            raise ValueError('SMR20 не подтвердил частоту, уровень или чистый CW')
        return state

    def enable(self, stop):
        if not self.prepared or stop.is_set():
            raise InterruptedError('SMR20 не подготовлен либо включение отменено')
        self.write(':OUTP ON')
        if int(self.query(':OUTP?')) != 1:
            raise RuntimeError('SMR20 не подтвердил RF ON')
        self.check_error()
        return self.state()

    def close(self):
        if self.device is not None:
            self.device.close()


def verified_smr_off(driver):
    try:
        driver.off()
        return driver.state()
    except Exception:
        driver.close()
        replacement = Smr20(driver.port, driver.baud, driver.log)
        try:
            replacement.off()
            return replacement.state()
        finally:
            replacement.close()


def lo_source(settings, log):
    if settings.get('lo_source', 'akip') == 'smr':
        return Smr20(settings.get('smr_port', 'COM3'), settings.get('smr_baud', 19200), log)
    from .ssg import Ssg3000x
    return Ssg3000x(settings['akip_host'], settings['akip_port'], log)


def verified_lo_off(driver):
    if isinstance(driver, Smr20):
        return verified_smr_off(driver)
    from .ssg import verified_ssg_off
    return verified_ssg_off(driver)
