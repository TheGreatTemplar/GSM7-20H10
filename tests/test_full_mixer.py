import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from gsmbench.rf_sequence import plan_sequence
from gsmbench.rsa import AnalyzerConfig
from tests.test_smu import CONTEXT, SETTINGS
from tools.full_mixer_test import BANDS, FullTest, p1db_search


class FullMixerTests(unittest.TestCase):
    def test_pause_boundary_does_not_start_next_band(self):
        run = object.__new__(FullTest)
        run.manifest = dict(analyzer_config={}, bands=BANDS)
        run.completed_bands = [160, 80, 60, 40]
        run.pause_after_band = 30
        run.watcher = MagicMock()
        run.done = MagicMock()
        run.checkpoint = MagicMock()
        run.run_band = MagicMock(side_effect=lambda band, low, high: run.completed_bands.append(band))
        run.folder = Path("unused-mocked-output")
        run.rows, run.estimates, run.errors = [], [], []
        run.last_state = dict(known=True)
        run.log_file, run.journal, run.csv_file = MagicMock(), MagicMock(), MagicMock()
        with patch("tools.full_mixer_test.atomic_json") as save, redirect_stdout(StringIO()):
            self.assertEqual(run.run(), 0)
        run.run_band.assert_called_once_with(30, 10100000, 10150000)
        self.assertEqual(save.call_args.args[1]["state"], "paused")
        self.assertEqual(run.completed_bands, [160, 80, 60, 40, 30])

    def test_requested_grid_has_2310_unique_points(self):
        keys = set()
        for band, low, high in BANDS:
            nodes = (low, (low + high) // 2, high)
            settings = dict(SETTINGS, sweep_kind="matrix", matrix_rf_mhz=";".join(str(hz / 1e6) for hz in nodes),
                            matrix_rf_dbm=";".join(str(power) for power in range(-60, 1, 10)),
                            matrix_lo_dbm=";".join(str(power) for power in range(10, 21)))
            plans = plan_sequence(AnalyzerConfig(), CONTEXT, settings, 231)
            self.assertEqual(len(plans), 231)
            for _, _, plan in plans:
                keys.add((band, round(plan.rf_hz), plan.rf_dbm, plan.lo_dbm))
        self.assertEqual(len(keys), 2310)

    def test_p1db_is_refined_and_never_exceeds_authorized_rf(self):
        for threshold in (-5, 0, 5, 9):
            measured = []

            def row(power):
                measured.append(power)
                loss = 6 + max(0, power - (threshold - 5)) * 0.2
                return dict(rf_dbm=power, output_dbm=power - loss, peak_above_background_db=50,
                            spectrum_id=str(power))

            result = p1db_search([row(power) for power in range(-60, 1, 10)], row)
            self.assertEqual(result["status"], "measured_uncalibrated")
            self.assertAlmostEqual(result["input_generator_p1db_dbm"], threshold, places=2)
            self.assertLessEqual(result["bracket_high_dbm"] - result["bracket_low_dbm"], 0.5)
            self.assertLessEqual(max(measured), 10)

    def test_missing_crossing_or_bad_reference_is_not_extrapolated(self):
        def row(power):
            return dict(rf_dbm=power, output_dbm=power - 6, peak_above_background_db=50, spectrum_id=str(power))
        coarse = [row(power) for power in range(-60, 1, 10)]
        result = p1db_search(coarse, row)
        self.assertEqual(result["status"], "not_reached")
        self.assertEqual(result["input_lower_bound_dbm"], 10)
        self.assertNotIn("input_generator_p1db_dbm", result)
        coarse[0]["output_dbm"] += 1
        self.assertEqual(p1db_search(coarse, row)["status"], "invalid_reference")

    def test_explicit_extended_limit_finds_crossing_above_ten_dbm(self):
        powers = []

        def row(power):
            powers.append(power)
            loss = 7 + max(0, power - 10) * 0.2
            return dict(rf_dbm=power, output_dbm=power - loss,
                        peak_above_background_db=45, spectrum_id=str(power))

        coarse = [row(power) for power in (-40, -30, -20, -10, 0)]
        estimate = p1db_search(coarse, row, rf_limit_dbm=20)
        self.assertAlmostEqual(estimate["input_generator_p1db_dbm"], 15)
        self.assertLessEqual(max(powers), 20)


if __name__ == "__main__":
    unittest.main()
