from dataclasses import asdict, replace
import math
import unittest

from gsmbench.two_tone import TwoTonePlan, analyze_two_tone, harmonic_band
from gsmbench.two_tone_ui import TwoToneWorker


def spectrum(plan, order=1):
    worker = TwoToneWorker(None, {}, [])
    result = worker.capture(None, plan, order)
    result["context"] = dict(method="two_tone_" + plan.mode, two_tone_plan=asdict(plan))
    return result


class TwoToneTests(unittest.TestCase):
    def test_pa_fourty_db_chain_and_seven_watt_limit(self):
        TwoTonePlan().validate()
        for plan in (replace(TwoTonePlan(), output_loss_db=20), replace(TwoTonePlan(), pad_rating_w=6),
                     replace(TwoTonePlan(), max_output_w=8), replace(TwoTonePlan(), level2_dbm=-18)):
            with self.assertRaises(ValueError):
                plan.validate()

    def test_average_power_pep_and_per_tone_ip3(self):
        plan = TwoTonePlan(calibrated=True)
        result = analyze_two_tone(spectrum(plan))
        self.assertEqual(result["status"], "preliminary", result)
        self.assertAlmostEqual(result["output_pep_w"], 2 * result["output_average_w"], places=8)
        self.assertAlmostEqual(result["sides"]["low"]["im3_dbc"], -45, delta=0.05)
        self.assertAlmostEqual(result["sides"]["low"]["ip3"]["iip3_dbm"], -3.5, delta=0.05)
        self.assertEqual(result["verdict"], "not_requested")

    def test_nominal_paths_do_not_produce_calibrated_ip3(self):
        result = analyze_two_tone(spectrum(TwoTonePlan()))
        self.assertIsNone(result["sides"]["low"]["ip3"])
        self.assertFalse(result["calibrated"])

    def test_noise_floor_is_not_an_im3_value(self):
        plan = replace(TwoTonePlan(), level1_dbm=-60, level2_dbm=-60)
        result = analyze_two_tone(spectrum(plan))
        self.assertEqual(result["status"], "preliminary", result)
        self.assertFalse(result["sides"]["low"]["resolved"])
        self.assertIsNone(result["sides"]["low"]["im3_dbc"])
        self.assertIsNone(result["sides"]["low"]["ip3"])

    def test_mixer_upper_lo_reverses_output_order_without_losing_spacing(self):
        plan = replace(TwoTonePlan(), mode="mixer", output_loss_db=0, harmonics=1, calibrated=True)
        low, high = sorted(plan.output_tones())
        self.assertEqual(high - low, plan.spacing_hz)
        self.assertEqual(plan.output_tones()[0], 11059200)
        result = analyze_two_tone(spectrum(plan))
        self.assertEqual(result["status"], "preliminary", result)

    def test_harmonic_band_peak_not_thd(self):
        plan = TwoTonePlan()
        band = harmonic_band(spectrum(plan, 2), plan, 2)
        self.assertEqual(band["order"], 2)
        self.assertTrue(plan.f1_hz * 2 - plan.rbw_hz * 3 <= band["frequency_hz"] <= plan.f2_hz * 2 + plan.rbw_hz * 3)
        self.assertTrue(math.isfinite(band["output_dbm"]))

    def test_bad_resolution_or_overload_invalidates_analysis(self):
        with self.assertRaises(ValueError):
            replace(TwoTonePlan(), rbw_hz=1000).validate()
        trace = spectrum(TwoTonePlan())
        trace["if_overload"] = True
        self.assertEqual(analyze_two_tone(trace)["status"], "not_calculated")

    def test_harmonic_noise_is_unresolved(self):
        plan = TwoTonePlan()
        trace = spectrum(plan, 2)
        trace["power_dbm"] = [-110] * len(trace["power_dbm"])
        band = harmonic_band(trace, plan, 2)
        self.assertFalse(band["resolved"])
