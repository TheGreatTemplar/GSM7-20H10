import copy
import unittest
import uuid

from gsmbench.rf_charts import chart_groups
from gsmbench.rsa import AnalyzerConfig, demo_spectrum


def sample(rf=14e6, lo_level=13):
    spectrum = demo_spectrum(AnalyzerConfig())
    spectrum["context"] = dict(sample="MX001", rf_hz=rf, lo_hz=rf + 11059200, if_hz=11059200,
        method="conversion", rf_actual_dbm=-20, lo_actual_dbm=lo_level, marker_window_hz=3000,
        pin_dbm=-21, pin_known=True, path_known=True, rf_loss_db=1, lo_loss_db=0, output_loss_db=1)
    return spectrum


class ChartTests(unittest.TestCase):
    def test_frequency_curve_keeps_lo_levels_separate(self):
        groups, skipped = chart_groups([sample(), sample(15e6), sample(lo_level=20)], "rf_hz", "raw_loss")
        self.assertEqual(skipped, 0)
        self.assertEqual(sorted(len(group["points"]) for group in groups), [1, 2])
        self.assertEqual(groups[0]["points"][0]["x"], 14)
        self.assertAlmostEqual(groups[0]["points"][0]["y"], 7)

    def test_repeats_average_only_identical_settings_and_export_ids(self):
        first = sample()
        second = copy.deepcopy(first)
        second["id"] = str(uuid.uuid4())
        second["power_dbm"] = [value + 1 for value in second["power_dbm"]]
        third = copy.deepcopy(first)
        third["id"] = str(uuid.uuid4())
        third["settings"]["attenuation_db"] += 10
        groups, skipped = chart_groups([first, second, third], "rf_hz", "raw_loss")
        self.assertEqual(len(groups), 2)
        point = groups[0]["points"][0]
        self.assertEqual(point["n"], 2)
        self.assertEqual(set(point["spectrum_ids"]), {first["id"], second["id"]})
        self.assertAlmostEqual(point["y"], 6.5)

    def test_corrected_result_and_overload_are_not_raw_values(self):
        good, bad = sample(), sample()
        bad["if_overload"] = True
        groups, skipped = chart_groups([good, bad], "rf_hz", "corrected")
        self.assertEqual(skipped, 1)
        self.assertAlmostEqual(groups[0]["points"][0]["y"], 5)
        good["context"]["path_known"] = False
        self.assertEqual(chart_groups([good], "rf_hz", "corrected"), ([], 1))

    def test_manual_spectra_can_show_output_without_known_generator_levels(self):
        spectrum = sample()
        del spectrum["context"]["rf_actual_dbm"]
        del spectrum["context"]["lo_actual_dbm"]
        groups, skipped = chart_groups([spectrum], "rf_hz", "output")
        self.assertEqual(skipped, 0)
        self.assertAlmostEqual(groups[0]["points"][0]["y"], -27)
