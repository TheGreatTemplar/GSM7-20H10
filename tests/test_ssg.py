import threading
import unittest
from unittest.mock import Mock, patch

from gsmbench.ssg import Ssg3000x, verified_ssg_off


class FakeSsg:
    def __init__(self):
        self.commands = []
        self.closed = False
        self.timeout = None
        self.bad_level = False
        self.values = {":OUTP?": "0", ":FREQ?": "3200000000", ":POW?": "-110", ":MOD?": "0",
                       ":SWE:STAT?": "OFF", ":CORR:FLAT?": "0", ":POW:OFFS?": "0"}

    def query(self, command):
        self.commands.append(command)
        if command == self.timeout:
            raise TimeoutError("SSG test timeout")
        if command == "*IDN?":
            return "RFSignal,RF Signal,SSG3XHDD800627,3.1.22"
        if command == ":SYST:ERR?":
            return '0,"No error"'
        if command == ":POW?" and self.bad_level:
            return "5"
        return self.values[command]

    def write(self, command):
        self.commands.append(command)
        key, value = command.split(" ", 1)
        if key in (":FREQ", ":POW"):
            value = value.split()[0]
        if key in (":OUTP", ":MOD", ":CORR:FLAT"):
            value = "1" if value in ("1", "ON") else "0"
        self.values[key + "?"] = value

    def close(self):
        self.closed = True


def make_ssg():
    device = FakeSsg()
    manager = Mock()
    manager.open_resource.return_value = device
    return Ssg3000x("127.0.0.1", manager_factory=Mock(return_value=manager)), device


class SsgTests(unittest.TestCase):
    def test_oem_identity_cw_setup_and_confirmed_on_off(self):
        driver, device = make_ssg()
        driver.prepare(14002000, -20)
        self.assertNotIn(":OUTP ON", device.commands)
        self.assertIn(":MOD?", device.commands)
        self.assertNotIn(":OUTP:MOD?", device.commands)
        driver.enable(threading.Event())
        self.assertEqual(device.values[":OUTP?"], "1")
        state = verified_ssg_off(driver)
        self.assertEqual(state["output"], 0)
        driver.close()
        self.assertTrue(device.closed)

    def test_bad_readback_and_stop_prevent_on(self):
        driver, device = make_ssg()
        device.bad_level = True
        with self.assertRaises(ValueError):
            driver.prepare(14002000, -20)
        self.assertNotIn(":OUTP ON", device.commands)
        device.bad_level = False
        driver.prepare(14002000, -20)
        stop = threading.Event()
        stop.set()
        with self.assertRaises(InterruptedError):
            driver.enable(stop)
        self.assertNotIn(":OUTP ON", device.commands)

    def test_timeout_requires_fresh_identified_off_connection(self):
        driver, device = make_ssg()
        device.timeout = ":OUTP?"
        with self.assertRaises(TimeoutError):
            driver.query(":OUTP?")
        with self.assertRaises(ConnectionError):
            driver.query(":FREQ?")
        fresh = Mock()
        fresh.state.return_value = dict(output=0)
        with patch("gsmbench.ssg.Ssg3000x", return_value=fresh):
            self.assertEqual(verified_ssg_off(driver), dict(output=0))
        fresh.off.assert_called_once()
        fresh.close.assert_called_once()
