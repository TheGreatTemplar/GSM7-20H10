import math
from pathlib import Path
import struct
import tempfile
import threading
import unittest

from gsmbench.e729 import E729, decode_measurement, impedance_parameters
from gsmbench.rlc_storage import RlcStore


def packet(frequency=1000000, resistance=10, reactance=100, flags=0x81):
    return bytes((0xAA, 72, flags, 0, 1, 3)) + struct.pack('>hIff', 0, frequency,
        math.hypot(resistance, reactance), math.atan2(reactance, resistance))


class FakeSerial:
    def __init__(self):
        self.buffer = bytearray()
        self.writes = []
        self.closed = False
        self.responses = [packet()]
        self.setting_ack = None
        self.is_open = False
        self.live_timeout_changes = 0

    def __setattr__(self, name, value):
        if name == 'timeout' and getattr(self, 'is_open', False):
            self.live_timeout_changes += 1
        super().__setattr__(name, value)

    def open(self):
        self.is_open = True

    def close(self):
        self.closed = True
        self.is_open = False

    def write(self, data):
        self.writes.append(data)
        if data == b'\xaa@':
            self.buffer.extend(b'\xaa@E729\0')
        elif data == b'\xaaH':
            self.buffer.extend(self.responses.pop(0))
        else:
            self.buffer.extend(data[:2] if self.setting_ack is None else self.setting_ack)
        return len(data)

    def read(self, count):
        # Force fragmented packets, including fragmented headers and float fields.
        count = min(count, 1)
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        return result


class E729Tests(unittest.TestCase):
    def test_series_and_parallel_inductance(self):
        result = impedance_parameters(1e6, math.hypot(10, 100), math.atan2(100, 10))
        self.assertAlmostEqual(result['rs_ohm'], 10)
        self.assertAlmostEqual(result['ls_h'], 100/(2*math.pi*1e6))
        self.assertAlmostEqual(result['rp_ohm'], 1010)
        self.assertAlmostEqual(result['lp_h'], 101/(2*math.pi*1e6))
        self.assertAlmostEqual(result['q'], 10)
        self.assertAlmostEqual(result['d'], 0.1)
        self.assertIsNone(result['cs_f'])

    def test_capacitance_and_zero_impedance(self):
        result = impedance_parameters(1e6, math.hypot(10, 100), math.atan2(-100, 10))
        self.assertAlmostEqual(result['cs_f'], 1/(2*math.pi*1e6*100))
        self.assertAlmostEqual(result['cp_f'], 100/(10100*2*math.pi*1e6))
        self.assertIsNone(result['ls_h'])
        self.assertIsNone(impedance_parameters(1e6, 0, 0)['rp_ohm'])

    def test_actual_instrument_packet(self):
        result = decode_measurement(bytes.fromhex('aa48910200040000009896803eaecb8a3fbe809b'))
        self.assertEqual(result['frequency_hz'], 10000000)
        self.assertEqual(result['equivalent_circuit'], 'parallel')
        self.assertAlmostEqual(result['values']['z_ohm'], 0.3413966, places=6)
        self.assertEqual(result['speed'], 0)
        self.assertAlmostEqual(result['values']['phase_deg'], 85.275, places=2)

    def test_overload_and_invalid_packets(self):
        result = decode_measurement(packet(flags=0x85))
        self.assertTrue(result['overloaded'])
        self.assertEqual(result['values'], {})
        for message in [packet()[:-1], packet(flags=1), packet(frequency=1)]:
            with self.assertRaises(ValueError):
                decode_measurement(message)
        with self.assertRaises(ValueError):
            impedance_parameters(1e6, float('nan'), 0)

    def test_fragmented_exchange_and_not_ready(self):
        fake = FakeSerial()
        meter = E729(serial_factory=lambda: fake)
        fake.responses = [b'\xaaH\0', packet(frequency=50000), packet(frequency=1000000)]
        result = meter.read_measurement(expected_frequency=1000000)
        self.assertEqual(result['frequency_hz'], 1000000)
        self.assertEqual(fake.writes, [b'\xaa@'] + [b'\xaaH'] * 3)
        self.assertEqual((fake.baudrate, fake.parity, fake.stopbits), (9600, 'N', 1))
        self.assertEqual(fake.live_timeout_changes, 0)
        meter.close()
        self.assertTrue(fake.closed)

    def test_configuration_binary_payload_and_cancel(self):
        fake = FakeSerial()
        meter = E729(serial_factory=lambda: fake)
        meter.set_frequency(15000000)
        meter.set_range(-1)
        meter.set_speed(1)
        self.assertEqual(fake.writes[1], b'\xaaC' + struct.pack('>I', 15000000))
        self.assertEqual(fake.writes[2:], [b'\xaaA', b'\xaaJ'])
        for invalid in [49999, 15000001, 50000.5]:
            with self.assertRaises(ValueError):
                meter.set_frequency(invalid)
        stop = threading.Event()
        stop.set()
        with self.assertRaises(InterruptedError):
            meter.read_measurement(stop)
        meter.close()

    def test_incomplete_packet_poisons_connection(self):
        fake = FakeSerial()
        meter = E729(serial_factory=lambda: fake, timeout=0.01)
        fake.responses = [packet()[:-2]]
        with self.assertRaises(TimeoutError):
            meter.read_measurement()
        with self.assertRaises(ConnectionError):
            meter.read_measurement()
        meter.close()

    def test_missing_setting_ack_requires_readback(self):
        fake = FakeSerial()
        fake.setting_ack = b''
        fake.responses = [packet(frequency=50000)]
        meter = E729(serial_factory=lambda: fake)
        meter.set_frequency(50000)
        self.assertEqual(fake.writes[-1], b'\xaaH')
        self.assertFalse(meter.broken)
        self.assertEqual(fake.live_timeout_changes, 0)
        meter.close()

    def test_corrupted_setting_ack_is_rejected(self):
        fake = FakeSerial()
        fake.setting_ack = b'\xaa\x83'
        meter = E729(serial_factory=lambda: fake)
        with self.assertRaises(ValueError):
            meter.set_frequency(50000)
        self.assertTrue(meter.broken)
        meter.close()

    def test_archive_preserves_raw_points_and_partial_series(self):
        with tempfile.TemporaryDirectory() as folder:
            store = RlcStore(Path(folder) / 'rlc.db')
            identifier = store.start(dict(sample='Катушка №1', demo=False))
            point = decode_measurement(packet())
            store.append(identifier, 0, point)
            store.finish(identifier, 'cancelled')
            reopened = RlcStore(store.path)
            self.assertEqual(reopened.points(identifier), [point])
            row = reopened.series()[0]
            self.assertEqual(row['count'], 1)
            self.assertEqual(row['status'], 'cancelled')
            self.assertEqual(row['settings']['sample'], 'Катушка №1')


if __name__ == '__main__':
    unittest.main()
