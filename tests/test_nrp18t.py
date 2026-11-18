import threading
import unittest
from unittest.mock import Mock

from gsmbench.nrp18t import Nrp18t
from gsmbench.rsa import AcquisitionCancelled


class NrpTests(unittest.TestCase):
    def make_meter(self):
        device = Mock()
        device.timeout = 3000
        answers = {"*IDN?": "ROHDE&SCHWARZ,NRP18T,103694,03.20.24090501", "*ESR?": "1",
            "SYST:ERR?": '0,"No error"', "SYST:SERR?": "0", "SENS:FUNC?": '"POW:AVG"',
            "UNIT:POW?": "W", "INIT:CONT?": "0", "SENS:AVER:COUN?": "16", "SENS:BUFF:STAT?": "0",
            "SENS:FREQ?": "14000000", "STAT:QUES:COND?": "0", "FETC?": "0.000001"}
        device.query.side_effect = lambda command: answers[command]
        manager = Mock()
        manager.open_resource.return_value = device
        return Nrp18t("USB-test", manager_factory=lambda: manager), device, manager

    def test_zero_native_scpi_units_and_readback(self):
        meter, device, manager = self.make_meter()
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        settings = meter.prepare(stop)
        device.write.assert_any_call("CAL:ZERO:AUTO ONCE")
        stop.wait.assert_any_call(10)
        self.assertTrue(settings["zero_confirmed"])
        self.assertEqual(device.timeout, 3000)
        self.assertAlmostEqual(meter.read_dbm(14e6, stop), -30)
        transition = meter.start_level(14e6, stop)
        device.write.assert_any_call("SENS:AVER:RES")
        self.assertAlmostEqual(transition["discarded_dbm"], -30)
        meter.close()
        device.close.assert_called_once()
        manager.close.assert_called_once()

    def test_cancel_and_overrange_do_not_produce_calibration_values(self):
        meter, device, _ = self.make_meter()
        stop = threading.Event()
        stop.set()
        with self.assertRaises(AcquisitionCancelled):
            meter.read_dbm(14e6, stop)
        stop.clear()
        original = device.query.side_effect
        device.query.side_effect = lambda command: "0.2" if command == "FETC?" else original(command)
        with self.assertRaises(ValueError):
            meter.read_dbm(14e6, stop)
        meter.close()
