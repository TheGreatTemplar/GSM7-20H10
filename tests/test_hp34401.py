import threading
import unittest
from unittest.mock import patch

from gsmbench.hp34401 import Hp34401a
from gsmbench.rsa import AcquisitionCancelled


class FakeSerial:
    def __init__(self, **kwargs):
        self.commands, self.received = [], bytearray()
        self.dsr, self.dtr, self.rts = True, True, True
        self.nplc = 10
        self.voltage = "0.123456"
        self.closed = False
        self.silent = False
        self.options = kwargs

    @property
    def in_waiting(self):
        return len(self.received)

    def write(self, data):
        self.commands.append(data)
        if data == b"\x03":
            self.received.clear()
            return len(data)
        text = data.decode().strip()
        if text.startswith("VOLT:DC:NPLC "):
            self.nplc = float(text.split()[-1])
        replies = {"*IDN?": "HEWLETT-PACKARD,34401A,0,11-5-2", "SYST:ERR?": '+0,"No error"',
            "FUNC?": '"VOLT"', "CONF?": '"VOLT +1.000000E+01,+3.000000E-06"',
            "VOLT:DC:NPLC?": str(self.nplc), "VOLT:DC:RES?": "0.0001", "READ?": self.voltage}
        if text in replies and not self.silent:
            self.received.extend((replies[text] + "\r\n").encode())
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        self.received.clear()

    def read(self, count):
        data = self.received[:count]
        del self.received[:count]
        return bytes(data)

    def close(self):
        self.closed = True


class MeterTests(unittest.TestCase):
    def test_requested_digits_and_actual_resolution_are_recorded(self):
        meter, serial = self.make_meter()
        settings = meter.configure_dc(10, 10, True, 5.5)
        self.assertIn(b"CONF:VOLT:DC 10,0.0001\n", serial.commands)
        self.assertEqual(settings["requested_digits"], 5.5)
        self.assertEqual(settings["actual_resolution_v"], 0.0001)
        self.assertEqual(settings["nplc"], 10)
        with patch("gsmbench.hp34401.time.sleep"):
            meter.close()

    def make_meter(self):
        serial = FakeSerial()
        with patch("gsmbench.hp34401.time.sleep"):
            meter = Hp34401a(serial_factory=lambda **kwargs: serial)
        return meter, serial

    def test_recovery_identity_dc_and_single_completed_queries(self):
        meter, serial = self.make_meter()
        settings = meter.configure_dc(10, 10, True)
        self.assertEqual(settings["function"], "VOLT")
        self.assertEqual(meter.read_voltage(threading.Event()), 0.123456)
        self.assertEqual(serial.commands[0], b"\x03")
        self.assertNotIn(b"*RST\n", serial.commands)
        with patch("gsmbench.hp34401.time.sleep"):
            meter.close()
        self.assertEqual(serial.commands[-1], b"SYST:LOC\n")
        self.assertTrue(serial.closed)

    def test_cancel_never_sends_a_new_read_and_overload_is_rejected(self):
        meter, serial = self.make_meter()
        meter.configure_dc()
        stop = threading.Event()
        stop.set()
        with self.assertRaises(AcquisitionCancelled):
            meter.read_voltage(stop)
        self.assertNotIn(b"READ?\n", serial.commands)
        serial.voltage = "9.9E37"
        with self.assertRaises(ValueError):
            meter.read_voltage(threading.Event())
        with patch("gsmbench.hp34401.time.sleep"):
            meter.close()

    def test_timeout_requires_recovery_before_another_query(self):
        meter, serial = self.make_meter()
        serial.silent = True
        with patch("gsmbench.hp34401.time.monotonic", side_effect=[0, 0, 0, 11]):
            with self.assertRaises(TimeoutError):
                meter.query("*IDN?")
        with self.assertRaises(ConnectionError):
            meter.query("*IDN?")
        with patch("gsmbench.hp34401.time.sleep"):
            meter.close()
        self.assertEqual(serial.commands.count(b"\x03"), 2)
