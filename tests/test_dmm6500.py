import socket
import threading
import unittest

from gsmbench.dmm6500 import Dmm6500
from gsmbench.rsa import AcquisitionCancelled


class FakeSocket:
    def __init__(self):
        self.commands = []
        self.reply = b""
        self.closed = False
        self.bad_nplc = False

    def settimeout(self, value):
        pass

    def sendall(self, data):
        command = data.decode("ascii").strip()
        self.commands.append(command)
        answers = {"*IDN?": "KEITHLEY INSTRUMENTS,MODEL DMM6500,TEST,1.7.12b", ":SYST:ERR?": '0,"No error"',
            ":SENS:FUNC?": '"VOLT:DC"', ":SENS:VOLT:NPLC?": "1" if self.bad_nplc else "10",
            ":DISP:VOLT:DIG?": "5", ":SENS:VOLT:INP?": "AUTO", ":SENS:VOLT:RANG:AUTO?": "0",
            ":SENS:VOLT:RANG?": "10", ":READ?": "0.123456789"}
        if "?" in command:
            self.reply = (answers[command] + "\n").encode("ascii")

    def recv(self, size):
        # Exercise fragmented replies rather than assuming one TCP packet per line.
        result, self.reply = self.reply[:3], self.reply[3:]
        return result

    def close(self):
        self.closed = True


class Dmm6500Tests(unittest.TestCase):
    def test_native_setup_readbacks_and_full_precision(self):
        device = FakeSocket()
        meter = Dmm6500(socket_factory=lambda *args, **kwargs: device)
        config = meter.configure_dc(10, 10, True, 5.5)
        self.assertEqual(config["display_digits"], 5.5)
        self.assertEqual(config["nplc"], 10)
        self.assertIn(":DISP:VOLT:DIG 5", device.commands)
        self.assertIn(":SENS:VOLT:INP AUTO", device.commands)
        self.assertAlmostEqual(meter.read_voltage(threading.Event()), 0.123456789)
        meter.close()
        self.assertTrue(device.closed)

    def test_unsupported_nplc_bad_readback_and_cancel(self):
        device = FakeSocket()
        meter = Dmm6500(socket_factory=lambda *args, **kwargs: device)
        with self.assertRaises(ValueError):
            meter.configure_dc(100)
        device.bad_nplc = True
        with self.assertRaises(ValueError):
            meter.configure_dc(digits=5.5)
        stop = threading.Event()
        stop.set()
        with self.assertRaises(AcquisitionCancelled):
            meter.query(":READ?", stop)
        self.assertNotIn(":READ?", device.commands)
        meter.close()
