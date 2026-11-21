import math
from pathlib import Path
import tempfile
import unittest

from gsmbench.e729 import decode_measurement
from gsmbench.rlc_uncertainty import coefficient_a1, measurement_uncertainty
from gsmbench.rlc_storage import RlcStore
from tests.test_e729 import packet


class RlcUncertaintyTests(unittest.TestCase):
    def test_a1_manual_values_and_boundaries(self):
        for frequency, z, selected_range, expected in [
                (50000, 1e5, 0, 1), (100000, 1e8, 0, 200.8),
                (50000, 10000, 1, .5), (1e6, 10000, 1, 1),
                (50000, 1000, 2, .3), (1e6, 1000, 2, .5), (2e6, 1000, 2, 2),
                (50000, 100, 3, .2), (1e6, 100, 3, .3), (2e6, 100, 3, .6),
                (50000, 10, 4, .56), (1e6, 10, 4, .75), (2e6, 10, 4, 2.8),
                (50000, 1, 5, 1.4), (1e6, 1, 5, 2.8), (50000, .01, 6, 20.8)]:
            self.assertAlmostEqual(coefficient_a1(frequency, z, selected_range), expected)
        for frequency, z, selected_range in [(100001, 1e5, 0), (1000001, 10000, 1),
                (2e6, 1, 5), (100001, .1, 6), (1e6, 9, 4)]:
            self.assertIsNone(coefficient_a1(frequency, z, selected_range))

    def test_manual_derived_limits_and_rectangular_model(self):
        point = decode_measurement(packet(frequency=1000000, resistance=10, reactance=100))
        # Selected 1 kOhm range, |Z|≈100.5 Ohm; A1=0.300249...
        result = measurement_uncertainty(point, 'UP-5', 1)
        self.assertEqual(result['status'], 'estimated')
        dz = .3 + .05*(point['values']['z_ohm']/100-1)
        self.assertAlmostEqual(result['impedance_error_limit_percent'], dz)
        limits = result['parameters']
        self.assertAlmostEqual(limits['ls_h']['error_limit_percent'], dz*1.1, places=5)
        self.assertAlmostEqual(limits['rs_ohm']['error_limit_percent'], dz*11, places=4)
        self.assertAlmostEqual(limits['q']['error_limit_abs'], dz/100*110, places=4)
        self.assertAlmostEqual(limits['d']['error_limit_abs'], dz/100*1.1, places=5)
        self.assertAlmostEqual(limits['phase_deg']['error_limit_abs'], dz)
        self.assertAlmostEqual(limits['ls_h']['expanded_uncertainty_k2_abs'],
            2*limits['ls_h']['error_limit_abs']/math.sqrt(3))

    def test_fixture_voltage_and_speed_multipliers(self):
        point = decode_measurement(packet(frequency=50000, resistance=100, reactance=0))
        one = measurement_uncertainty(point, 'UP-5', 1)['impedance_error_limit_percent']
        for voltage, multiplier in [(.04, 10), (.1, 10), (.100001, 3), (.3, 3), (.300001, 1), (1, 1)]:
            self.assertAlmostEqual(measurement_uncertainty(point, 'UP-5', voltage)['impedance_error_limit_percent'], one*multiplier)
        up2 = measurement_uncertainty(point, 'UP-2', 1)
        self.assertAlmostEqual(up2['coefficients']['A4'], 2.25)
        point['speed'] = 0
        self.assertAlmostEqual(measurement_uncertainty(point, 'UP-9', 1)['impedance_error_limit_percent'], one*3)

    def test_unsupported_conditions_and_negative_resistance_are_explicit(self):
        point = decode_measurement(packet())
        for fixture, level in [(None, 1), ('other', 1), ('UP-5', None), ('UP-2', 1)]:
            result = measurement_uncertainty(point, fixture, level)
            self.assertEqual(result['status'], 'unavailable')
            self.assertTrue(result['reason'])
        point = decode_measurement(packet(resistance=-1, reactance=100))
        self.assertEqual(measurement_uncertainty(point, 'UP-5', 1)['status'], 'unavailable')

    def test_recalculation_keeps_original_raw_measurements(self):
        with tempfile.TemporaryDirectory() as folder:
            store = RlcStore(Path(folder) / 'rlc.db')
            identifier = store.start(dict(sample='Дроссель'))
            original = decode_measurement(packet())
            store.append(identifier, 0, original)
            store.recalculate_uncertainty(identifier, 'UP-5', 1)
            saved = store.points(identifier)[0]
            self.assertEqual(saved['raw_hex'], original['raw_hex'])
            self.assertEqual(saved['values'], original['values'])
            self.assertEqual(saved['measured_at'], original['measured_at'])
            self.assertEqual(saved['uncertainty']['status'], 'estimated')
            self.assertEqual(store.series()[0]['settings']['fixture_id'], 'UP-5')


if __name__ == '__main__':
    unittest.main()
