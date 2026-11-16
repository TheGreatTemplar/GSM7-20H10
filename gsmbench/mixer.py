"""Mixer measurement calculations, independent of instrument firmware and UI.

All powers here are at DUT reference planes, after cable/pad calibration.
No function in this module connects to hardware or enables an RF output.
"""

from dataclasses import dataclass
import math


def finite(*values):
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Частоты, уровни и поправки должны быть конечными числами")


@dataclass(frozen=True)
class PathLoss:
    """Positive losses, in dB, at a particular measurement frequency."""

    input_db: float = 0
    output_db: float = 0

    def at_dut(self, source_dbm, analyzer_dbm):
        finite(source_dbm, analyzer_dbm, self.input_db, self.output_db)
        if min(self.input_db, self.output_db) < 0:
            raise ValueError("Потери кабелей и аттенюаторов задаются положительными")
        return source_dbm - self.input_db, analyzer_dbm + self.output_db


def difference_if(rf_hz, lo_hz):
    finite(rf_hz, lo_hz)
    if min(rf_hz, lo_hz) <= 0 or rf_hz == lo_hz:
        raise ValueError("Нужны положительные разные RF и LO для ненулевой разностной ПЧ")
    return abs(rf_hz - lo_hz)


def conversion_loss(source_dbm, analyzer_dbm, path=PathLoss()):
    """Positive loss / negative gain. No absolute value: active mixers can gain."""
    pin, pout = path.at_dut(source_dbm, analyzer_dbm)
    return pin - pout


def isolation(source_dbm, leakage_dbm, path=PathLoss()):
    """Same-frequency isolation; both powers must refer to the injected tone."""
    pin, leakage = path.at_dut(source_dbm, leakage_dbm)
    return pin - leakage


def ip3(pin_per_tone_dbm, fundamental_dbm, im3_dbm):
    """Single-side IIP3/OIP3 from a resolved fundamental and its IM3 product.

    Call separately for both sides. Inputs must already be corrected for their
    individual frequencies. Noise-floor/overload validity is the caller's job.
    """
    finite(pin_per_tone_dbm, fundamental_dbm, im3_dbm)
    if im3_dbm >= fundamental_dbm:
        raise ValueError("IM3 должен быть ниже основного тона; оценка IP3 недостоверна")
    separation = fundamental_dbm - im3_dbm
    return dict(iip3_dbm=pin_per_tone_dbm + separation / 2,
                oip3_dbm=fundamental_dbm + separation / 2)


def compression_1db(points, reference_count=3):
    """Interpolate the first 1 dB crossing relative to measured small-signal gain.

    points: increasing (Pin_dBm, Pout_dBm), at calibrated DUT reference planes.
    None means the crossing was not reached, not a zero or extrapolated result.
    The first reference_count samples must be in the small-signal linear region.
    """
    if type(reference_count) is not int or reference_count < 2 or len(points) <= reference_count:
        raise ValueError("Для P1dB нужны минимум две опорные точки и следующая точка серии")
    for pin, pout in points:
        finite(pin, pout)
    if any(right[0] <= left[0] for left, right in zip(points, points[1:])):
        raise ValueError("Уровни Pin для P1dB должны строго возрастать")
    gains = [pout - pin for pin, pout in points]
    reference = gains[:reference_count]
    if max(reference) - min(reference) > 0.25:
        raise ValueError("Опорный участок нелинеен: выберите более низкие уровни RF")
    gain = sum(reference) / reference_count
    for index in range(reference_count, len(points)):
        previous, current = gain - gains[index - 1], gain - gains[index]
        if previous < 1 <= current:
            fraction = (1 - previous) / (current - previous)
            pin = points[index - 1][0] + fraction * (points[index][0] - points[index - 1][0])
            return dict(input_p1db_dbm=pin, output_p1db_dbm=pin + gain - 1,
                        small_signal_gain_db=gain)
    return None
