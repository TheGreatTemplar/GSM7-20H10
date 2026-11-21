import unittest

from gsmbench.smr import Smr20


class SerialStub:
    def __init__(self, *args, **kwargs):
        self.settings = kwargs
        self.commands = []
        self.response = b'ROHDE&SCHWARZ,SMR20,101670/020,1.44\r\n'
        self.closed = False

    def write(self, value):
        self.commands.append(value)

    def read_until(self, terminator):
        return self.response

    def close(self):
        self.closed = True


class SmrTests(unittest.TestCase):
    def test_identification_uses_serial_framing_without_rf_commands(self):
        driver = Smr20(serial_factory=SerialStub)
        self.assertEqual(driver.device.commands, [b'*IDN?\n'])
        self.assertEqual(driver.device.settings['stopbits'], 2)
        self.assertTrue(driver.device.settings['rtscts'])
        self.assertEqual(driver.device.settings['baudrate'], 19200)
        driver.close()

    def test_incomplete_read_poisoned_connection_never_consumes_stale_reply(self):
        driver = Smr20(serial_factory=SerialStub)
        driver.device.response = b'1'
        with self.assertRaises(TimeoutError):
            driver.query(':OUTP?')
        count = len(driver.device.commands)
        with self.assertRaises(ConnectionError):
            driver.query(':FREQ?')
        self.assertEqual(len(driver.device.commands), count)
        driver.close()

    def test_out_of_range_frequency_is_rejected_before_write(self):
        driver = Smr20(serial_factory=SerialStub)
        driver.prepared = True
        driver.bounds = dict(fmin=10e6, fmax=20e9, pmin=-130, pmax=20)
        with self.assertRaises(ValueError):
            driver.setpoint(1.81e6, -20)
        self.assertEqual(driver.device.commands, [b'*IDN?\n'])
        driver.close()
