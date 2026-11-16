import math
import unittest
from unittest.mock import Mock

from gsmbench.mixer import PathLoss, compression_1db, conversion_loss, difference_if, ip3, isolation
from gsmbench.rf_instruments import identify_resource, lan_resource


class MixerTests(unittest.TestCase):
    def test_reference_plane_correction_and_active_gain(self):
        self.assertEqual(conversion_loss(-10, -22, PathLoss(2, 3)), 7)
        self.assertEqual(conversion_loss(-10, -5), -5)
        self.assertEqual(isolation(13, -45, PathLoss(1, 2)), 55)
        with self.assertRaises(ValueError):
            PathLoss(-1, 0).at_dut(0, -10)

    def test_if_and_invalid_numbers(self):
        self.assertEqual(difference_if(14e6, 23e6), 9e6)
        for value in (math.nan, math.inf):
            with self.assertRaises(ValueError):
                conversion_loss(value, 0)
        with self.assertRaises(ValueError):
            difference_if(14e6, 14e6)

    def test_ip3_uses_per_tone_input_and_conversion_gain(self):
        result = ip3(-20, -27, -77)
        self.assertEqual(result, dict(iip3_dbm=5, oip3_dbm=-2))
        with self.assertRaises(ValueError):
            ip3(-20, -27, -20)

    def test_compression_interpolation_and_unreached_crossing(self):
        points = [(-30, -37), (-25, -32), (-20, -27), (-15, -22.5), (-10, -18.5)]
        result = compression_1db(points)
        self.assertAlmostEqual(result["input_p1db_dbm"], -12.5)
        self.assertAlmostEqual(result["output_p1db_dbm"], -20.5)
        self.assertIsNone(compression_1db(points[:-1]))
        with self.assertRaises(ValueError):
            compression_1db(points[::-1])
        with self.assertRaises(ValueError):
            compression_1db([(-30, -37), (-25, -33), (-20, -29), (-15, -25)])

    def test_lan_identity_probe_cannot_change_rf_state(self):
        manager = Mock()
        device = manager.open_resource.return_value
        device.query.side_effect = ["Rohde&Schwarz,SMU200A,1234,5.0", "B103,B203"]
        factory = Mock(return_value=manager)
        result = identify_resource(lan_resource("172.16.236.10"), "SMU200A", True, factory)
        self.assertEqual(result["options"], "B103,B203")
        self.assertEqual([call.args[0] for call in device.query.call_args_list], ["*IDN?", "*OPT?"])
        device.write.assert_not_called()
        device.close.assert_called_once()
        manager.close.assert_called_once()

    def test_identity_mismatch_and_failed_open_release_connections(self):
        manager = Mock()
        manager.open_resource.return_value.query.return_value = "RIGOL,RSA5065,1,2"
        with self.assertRaises(ValueError):
            identify_resource(lan_resource("172.16.236.10"), "RSA6265", manager_factory=Mock(return_value=manager))
        manager.open_resource.return_value.close.assert_called_once()
        manager.close.assert_called_once()
        manager = Mock()
        manager.open_resource.side_effect = TimeoutError("no instrument")
        with self.assertRaises(TimeoutError):
            identify_resource(lan_resource("172.16.236.10"), "RSA6265", manager_factory=Mock(return_value=manager))
        manager.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
