"""Two-tone planning and trace analysis, independent of output switching."""

from dataclasses import dataclass
import math
import re
import statistics

from .mixer import ip3
from .rsa import AnalyzerConfig, frequencies, peak_near
from .validation import FieldError


def parse_frequency_ranges(text, maximum_points=201):
    """Parse MHz entries such as '1.810..2.000/0.095; 50..52/1'."""
    if not text.strip():
        return []
    frequencies = []
    for entry in re.split(r"[;\r\n]+", text):
        entry = entry.strip().replace(",", ".")
        if not entry:
            continue
        if "/" in entry:
            if entry.count("/") != 1:
                raise FieldError("Формат диапазона: начало..конец/шаг", "frequency_ranges")
            range_text, step_text = (part.strip() for part in entry.split("/", 1))
            match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*(?:\.\.|[–—-])\s*([0-9]+(?:\.[0-9]+)?)\s*", range_text)
            if not match:
                raise FieldError("Формат диапазона: начало..конец/шаг", "frequency_ranges")
            try:
                start, stop, step = float(match[1]), float(match[2]), float(step_text)
            except ValueError as error:
                raise FieldError("Шаг частоты должен быть числом в МГц", "frequency_ranges") from error
            if not all(math.isfinite(value) for value in (start, stop, step)) or start <= 0 or stop < start or step <= 0:
                raise FieldError("В диапазоне нужны положительные частоты и шаг", "frequency_ranges")
            count = math.floor((stop - start) / step + 1e-9) + 1
            if count > maximum_points:
                raise FieldError(f"В списке допускается не более {maximum_points} частот", "frequency_ranges")
            values = [start + index * step for index in range(count)]
            if not math.isclose(values[-1], stop, abs_tol=1e-9, rel_tol=0):
                values.append(stop)
            frequencies.extend(values)
        else:
            try:
                value = float(entry)
            except ValueError as error:
                raise FieldError("Укажите частоту в МГц или диапазон начало..конец/шаг", "frequency_ranges") from error
            if not math.isfinite(value) or value <= 0:
                raise FieldError("Частоты должны быть положительными", "frequency_ranges")
            frequencies.append(value)
        if len(frequencies) > maximum_points:
            raise FieldError(f"В списке допускается не более {maximum_points} частот", "frequency_ranges")
    frequencies = [value * 1e6 for value in frequencies]
    if len(set(frequencies)) != len(frequencies):
        raise FieldError("В диапазонах есть повторяющиеся частоты", "frequency_ranges")
    if any(not 0.1e6 <= value <= 200e6 for value in frequencies):
        raise FieldError("Частоты RF должны быть в диапазоне 0,1…200 МГц", "frequency_ranges")
    return frequencies


def parse_level_ranges(text, fallback, maximum_points=201):
    """Parse optional dBm entries and ranges such as '10..20/1; 13'."""
    if not text.strip():
        return [fallback]
    values = []
    for entry in text.split(";"):
        entry = entry.strip().replace(",", ".")
        if not entry:
            continue
        if "/" not in entry:
            try:
                values.append(float(entry))
            except ValueError as error:
                raise FieldError("Формат уровня: число или начало..конец/шаг", "lo_levels") from error
            continue
        if entry.count("/") != 1:
            raise FieldError("Формат уровня: начало..конец/шаг", "lo_levels")
        range_text, step_text = (part.strip() for part in entry.split("/", 1))
        match = re.fullmatch(r"\s*(-?[0-9]+(?:\.[0-9]+)?)\s*\.\.\s*(-?[0-9]+(?:\.[0-9]+)?)\s*", range_text)
        if not match:
            raise FieldError("Формат уровня: начало..конец/шаг", "lo_levels")
        try:
            start, stop, step = float(match[1]), float(match[2]), float(step_text)
        except ValueError as error:
            raise FieldError("Шаг уровня должен быть числом в dBm", "lo_levels") from error
        if not all(math.isfinite(value) for value in (start, stop, step)) or stop < start or step <= 0:
            raise FieldError("Для уровня нужен возрастающий диапазон и положительный шаг", "lo_levels")
        count = math.floor((stop - start) / step + 1e-9) + 1
        if count > maximum_points:
            raise FieldError(f"Допускается не более {maximum_points} уровней LO", "lo_levels")
        sequence = [start + index * step for index in range(count)]
        if not math.isclose(sequence[-1], stop, abs_tol=1e-9, rel_tol=0):
            sequence.append(stop)
        values.extend(sequence)
        if len(values) > maximum_points:
            raise FieldError(f"Допускается не более {maximum_points} уровней LO", "lo_levels")
    if not values or any(not math.isfinite(value) or not -110 <= value <= 20 for value in values):
        raise FieldError("Уровни LO должны быть в диапазоне −110…+20 dBm", "lo_levels")
    if len(set(values)) != len(values):
        raise FieldError("В списке уровней LO есть повторяющиеся значения", "lo_levels")
    return values


@dataclass(frozen=True)
class TwoTonePlan:
    mode: str = "pa"
    f1_hz: float = 14000000
    spacing_hz: float = 2000
    lo_hz: float = 25059200
    level1_dbm: float = -20
    level2_dbm: float = -20
    lo_dbm: float = 13
    loss1_db: float = 6
    loss2_db: float = 6
    output_loss_db: float = 40
    pad_rating_w: float = 10
    max_output_w: float = 7
    rbw_hz: float = 100
    attenuation_db: int = 20
    reference_dbm: float = 10
    points: int = 3201
    harmonics: int = 5
    calibrated: bool = False
    sweep_averages: int = 1
    detector: str = "POS"

    @property
    def f2_hz(self):
        return self.f1_hz + self.spacing_hz

    def output_tones(self):
        return ([self.f1_hz, self.f2_hz] if self.mode == "pa" else
                [abs(self.lo_hz - self.f1_hz), abs(self.lo_hz - self.f2_hz)])

    def validate(self):
        values = (self.f1_hz, self.spacing_hz, self.lo_hz, self.level1_dbm, self.level2_dbm, self.lo_dbm,
                  self.loss1_db, self.loss2_db, self.output_loss_db, self.pad_rating_w, self.max_output_w, self.rbw_hz)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Параметры двухтонового задания должны быть конечными")
        if self.mode not in ("pa", "mixer") or not 100000 <= self.f1_hz <= 200e6 or not self.f1_hz < self.f2_hz <= 200.1e6:
            raise FieldError("Первый RF-тон: 0,1…200 МГц; второй — выше на заданный интервал", "f1_mhz", "spacing_hz")
        if not 100 <= self.spacing_hz <= 100000:
            raise FieldError("Интервал тонов: 100 Гц…100 кГц", "spacing_hz")
        if self.spacing_hz < 10 * self.rbw_hz:
            raise FieldError("Для разделения тонов задайте интервал не менее 10 RBW; используйте предустановку RSA для двух тонов", "spacing_hz", "rbw_hz")
        if not all(-110 <= value <= 20 for value in (self.level1_dbm, self.level2_dbm, self.lo_dbm)):
            raise ValueError("Уровни источников: −110…+20 dBm с подтверждением прибором")
        if min(self.loss1_db, self.loss2_db, self.output_loss_db) < 0:
            raise ValueError("Потери трактов должны быть неотрицательны")
        if abs((self.level1_dbm - self.loss1_db) - (self.level2_dbm - self.loss2_db)) > 0.5:
            raise FieldError("Для двухтонового IP3 выровняйте уровни на входе DUT до разности не более 0,5 дБ", "level1", "level2", "loss1", "loss2")
        if type(self.harmonics) is not int or not 1 <= self.harmonics <= 7:
            raise ValueError("Гармоники: 1 (без гармоник) … 7")
        if type(self.sweep_averages) is not int or not 1 <= self.sweep_averages <= 50:
            raise ValueError("Усреднение: 1…50 независимых развёрток на полосу")
        low, high = sorted(self.output_tones())
        if 2 * low - high <= 0:
            raise ValueError("Нижний IM3 должен иметь положительную частоту")
        if self.mode == "pa":
            if not 0 < self.max_output_w <= 7:
                raise ValueError("Для этого PA предел средней выходной мощности должен быть до 7 Вт")
            if self.pad_rating_w < self.max_output_w:
                raise FieldError("Допустимая мощность первого аттенюатора меньше предела мощности PA", "pad_rating", "max_w")
            # Ref Level is a spectral level, not the time-domain two-tone PEP.
            # Use total average power as a conservative bound for either tone.
            predicted_tone_input = 10 * math.log10(self.max_output_w * 1000) - self.output_loss_db
            if self.output_loss_db < 20 or predicted_tone_input > self.reference_dbm - 3 + 1e-9:
                maximum = 10 ** ((self.reference_dbm - 3 + self.output_loss_db - 30) / 10)
                raise FieldError(f"Для этого ослабления и Ref Level задайте предел средней мощности не выше {maximum:.4g} Вт "
                    "либо увеличьте ослабление до RSA. Нужен запас 3 дБ до Ref Level.", "output_loss", "max_w", "reference_dbm")
        for order in range(1, (self.harmonics if self.mode == "pa" else 1) + 1):
            config = self.analyzer_config(order)
            config.validate()
            if config.span_hz / (config.points - 1) > self.rbw_hz / 2:
                raise FieldError("Для выбранного интервала/гармоник увеличьте RBW или уменьшите интервал тонов", "spacing_hz", "rbw_hz", "points")

    def analyzer_config(self, order=1):
        low, high = sorted(self.output_tones())
        span = max(2000, 12 * self.spacing_hz * order)
        points = max(self.points, math.ceil(2 * span / self.rbw_hz) + 1) if order > 1 else self.points
        return AnalyzerConfig(center_hz=(low + high) / 2 * order, span_hz=span,
            rbw_hz=self.rbw_hz, vbw_hz=self.rbw_hz, attenuation_db=self.attenuation_db,
            reference_dbm=self.reference_dbm, points=points, detector=self.detector)


def watts(dbm):
    return 10 ** ((dbm - 30) / 10)


def analyze_two_tone(spectrum):
    try:
        context = spectrum["context"]
        plan = TwoTonePlan(**context["two_tone_plan"])
        plan.validate()
        settings = spectrum["settings"]
        if spectrum.get("if_overload") or any(settings.get(key, 0) for key in
                ("amplitude_correction", "reference_offset_db", "external_gain_db")):
            raise ValueError("Перегрузка либо амплитудные поправки RSA")
        if settings["trace_type"] != "WRIT":
            raise ValueError("Нужна новая трасса Clear/Write")
        if (settings["stop_hz"] - settings["start_hz"]) / (len(spectrum["power_dbm"]) - 1) > settings["rbw_hz"] / 2:
            raise ValueError("Недостаточное разрешение частотной сетки")
        low, high = sorted(plan.output_tones())
        targets = (low, high, 2 * low - high, 2 * high - low)
        window = min(plan.spacing_hz * 0.2, plan.rbw_hz * 3)
        markers = {name: peak_near(spectrum, target, window) for name, target in
                   zip(("fund_low", "fund_high", "im3_low", "im3_high"), targets)}
        bins = frequencies(spectrum)
        background = [power for frequency, power in zip(bins, spectrum["power_dbm"])
                      if all(abs(frequency - target) > max(window, 4 * plan.rbw_hz) for target in targets)]
        if not background:
            raise ValueError("Нет свободных точек для оценки шумового пола")
        noise = statistics.median(background)
        if min(markers[name]["power_dbm"] - noise for name in ("fund_low", "fund_high")) < 20:
            raise ValueError("Основные тоны недостаточно выше шумового пола")
        if max(marker["power_dbm"] for marker in markers.values()) >= settings["reference_dbm"] - 0.5:
            raise ValueError("Тон достигает опорного уровня RSA")
        p_low = markers["fund_low"]["power_dbm"] + plan.output_loss_db
        p_high = markers["fund_high"]["power_dbm"] + plan.output_loss_db
        pins = [plan.level1_dbm - plan.loss1_db, plan.level2_dbm - plan.loss2_db]
        if plan.output_tones()[0] > plan.output_tones()[1]:
            pins.reverse()
        sides = {}
        for side, fundamental, pin in (("low", p_low, pins[0]), ("high", p_high, pins[1])):
            product = markers["im3_" + side]["power_dbm"] + plan.output_loss_db
            resolved = markers["im3_" + side]["power_dbm"] - noise >= 10
            sides[side] = dict(resolved=resolved, im3_dbc=product - fundamental if resolved else None,
                               noise_limit_dbc=noise + 10 + plan.output_loss_db - fundamental,
                               fundamental_dbm=fundamental, product_dbm=product, pin_dbm=pin,
                               ip3_estimate=ip3(pin, fundamental, product) if resolved else None,
                               ip3=ip3(pin, fundamental, product) if resolved and plan.calibrated else None)
        total = watts(p_low) + watts(p_high)
        pep = (math.sqrt(watts(p_low)) + math.sqrt(watts(p_high))) ** 2
        resolved_values = [-item["im3_dbc"] for item in sides.values() if item["resolved"]]
        return dict(kind="two_tone", status="preliminary", method=context["method"],
            value_db=min(resolved_values) if resolved_values else None, marker=markers["fund_low"], markers=markers,
            sides=sides, noise_dbm=noise, output_average_w=total, output_pep_w=pep,
            fundamental_low_dbm=p_low, fundamental_high_dbm=p_high, calibrated=plan.calibrated,
            ip3_basis="confirmed_paths" if plan.calibrated else "nominal_paths_estimate",
            verdict="not_requested", reason="Предварительно; PEP по двум основным тонам, гармонические полосы сохраняются отдельно")
    except (KeyError, TypeError, ValueError) as error:
        return dict(kind="two_tone", status="not_calculated", reason=str(error))


def harmonic_band(spectrum, plan, order):
    low, high = sorted(plan.output_tones())
    bins = frequencies(spectrum)
    window = plan.rbw_hz * 3
    indices = [index for index, value in enumerate(bins) if order * low - window <= value <= order * high + window]
    if not indices or spectrum.get("if_overload"):
        raise ValueError("Нет гармонической полосы либо перегрузка RSA")
    index = max(indices, key=lambda item: spectrum["power_dbm"][item])
    background = [power for frequency, power in zip(bins, spectrum["power_dbm"])
                  if frequency < order * low - 4 * window or frequency > order * high + 4 * window]
    if not background:
        raise ValueError("Нет свободных точек для оценки шумового пола гармонической полосы")
    noise = statistics.median(background)
    return dict(order=order, frequency_hz=bins[index], rsa_dbm=spectrum["power_dbm"][index],
                output_dbm=spectrum["power_dbm"][index] + plan.output_loss_db,
                noise_dbm=noise, resolved=spectrum["power_dbm"][index] - noise >= 10)
