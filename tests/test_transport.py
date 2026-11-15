import threading
import unittest
from unittest.mock import patch

from gsmbench.instrument import LineTransport, MeasurementCancelled, VisaTransport


class DelayedTransport(LineTransport):
    def __init__(self, stop):
        self.stop = stop
        self.buffer = b""
        self.pending_query = None
        self.label = "test"
        self.commands = []
        self.replies = []
        self.log = lambda message: None

    def write(self, command):
        self.commands.append(command)
        if command == ":ABOR":
            self.replies.extend([b"1", b"\n"])
        elif command == ":OUTP?":
            self.replies.append(b"0\n")
        elif command == "*IDN?":
            self.replies.append(b"PRIST,GSM7-20H10,012345678,V1.24\n")

    def read_chunk(self, timeout):
        if not self.replies:
            self.stop.set()
            return None
        return self.replies.pop(0)


class TransportTests(unittest.TestCase):
    def test_cancel_writes_before_reply_and_drains_original_response(self):
        stop = threading.Event()
        transport = DelayedTransport(stop)

        def cancel():
            transport.write(":ABOR")
            transport.write(":OUTP OFF")

        with self.assertRaises(MeasurementCancelled):
            transport.query(":TRAC:POIN:ACT?", timeout=30, stop=stop, on_cancel=cancel)
        self.assertEqual(transport.commands, [":TRAC:POIN:ACT?", ":ABOR", ":OUTP OFF", "*IDN?"])
        self.assertIsNone(transport.pending_query)
        self.assertEqual(transport.query(":OUTP?"), "0")

    def test_timed_out_query_cannot_contaminate_output_confirmation(self):
        transport = DelayedTransport(threading.Event())
        with patch("gsmbench.instrument.time.monotonic", side_effect=[0, 4]):
            with self.assertRaises(TimeoutError):
                transport.query(":TRAC:POIN:ACT?")
        with self.assertRaises(ConnectionError):
            transport.query(":OUTP?")
        self.assertNotIn(":OUTP?", transport.commands)

    def test_visa_partial_reply_survives_short_timeouts(self):
        import pyvisa
        transport = VisaTransport.__new__(VisaTransport)
        transport.buffer = b""
        transport.pending_query = None
        transport.label = "VISA"
        transport.log = lambda message: None

        class Device:
            timeout = 3000

            def __init__(self):
                self.data = iter([b"2", None, b"0", None, b"\n"])
                self.writes = []

            def write(self, command):
                self.writes.append(command)

            def read_bytes(self, count, break_on_termchar):
                self.assertions = (count, break_on_termchar)
                data = next(self.data)
                if data is None:
                    raise pyvisa.errors.VisaIOError(pyvisa.constants.StatusCode.error_timeout)
                return data

        transport.device = Device()
        self.assertEqual(transport.query(":TRAC:POIN:ACT?", timeout=30), "20")
        self.assertEqual(transport.device.writes, [":TRAC:POIN:ACT?"])
        self.assertIsNone(transport.pending_query)


if __name__ == "__main__":
    unittest.main()
