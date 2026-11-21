"""Manual-based uncertainty estimates for stored RF calibration and measurements."""

import math
import statistics


def rsa_level_standard_uncertainty(frequency_hz):
    """Return the RSA6265 level standard uncertainty inferred from its manual."""
    if frequency_hz > 10e6:
        return math.sqrt((0.8 / 2) ** 2 + (0.3 / math.sqrt(3)) ** 2)
    return math.sqrt((0.3 ** 2 + 0.5 ** 2 + 0.3 ** 2) / 3)


def _type_a_from_sweeps(spectrum):
    averaging = spectrum.get("averaging", {})
    count = averaging.get("count", 0)
    standard_deviation = averaging.get("marker_std_db")
    if count > 1 and standard_deviation is not None:
        return standard_deviation / math.sqrt(count)
    return None


def _calibrated_source_standard_uncertainty(spectrum):
    context = spectrum.get("context", {})
    plan = context.get("two_tone_plan", {})
    if not plan:
        return None
    f1_hz = plan["f1_hz"]
    f2_hz = f1_hz + plan["spacing_hz"]
    profiles = []
    selected = context.get("source_calibration")
    if selected:
        if selected.get("f1_hz") is not None and selected.get("f2_hz") is not None:
            profiles.append((selected, selected["f1_hz"], selected["f2_hz"]))
        profiles.extend((item, item.get("f1_hz"), item.get("f2_hz"))
            for item in selected.get("frequency_profiles", []))
    totals = []
    for channel, frequency, target in (
            (0, f1_hz, plan["level1_dbm"] - plan["loss1_db"]),
            (1, f2_hz, plan["level2_dbm"] - plan["loss2_db"])):
        def matches_frequency(item, f1, f2):
            pair_match = math.isclose(f1, f1_hz, abs_tol=0.05, rel_tol=0) and math.isclose(
                f2, f2_hz, abs_tol=0.05, rel_tol=0)
            single_match = math.isclose(f1, frequency, abs_tol=0.05, rel_tol=0) and math.isclose(
                f2, frequency, abs_tol=0.05, rel_tol=0)
            has_level = any(math.isclose(point.get("target_dbm", math.nan), target, abs_tol=1e-6, rel_tol=0)
                for point in item.get("points", []))
            return has_level and (pair_match or single_match)

        profile = next((item for item, f1, f2 in profiles
            if f1 is not None and f2 is not None and matches_frequency(item, f1, f2)), None)
        if profile is None:
            return None
        point = next(point for point in profile["points"]
            if math.isclose(point.get("target_dbm", math.nan), target, abs_tol=1e-6, rel_tol=0))
        channels = point.get("uncertainty", {}).get("channels", [])
        if channel >= len(channels):
            return None
        component = channels[channel]
        uncertainty = component.get("combined_standard_uncertainty_db")
        if uncertainty is None:
            expanded = component.get("expanded_uncertainty_k2_db")
            uncertainty = expanded / 2 if expanded is not None else None
        if uncertainty is None:
            return None
        totals.append(uncertainty)
    return math.sqrt(sum(value ** 2 for value in totals)) if totals else None


def result_uncertainty(spectrum, result):
    """Estimate uncertainty of a stored dB result and record missing components."""
    if result.get("status") != "preliminary" or (result.get("value_db") is None and result.get("kind") != "two_tone"):
        return dict(status="not_available", reason="Нет численного результата для расчёта неопределённости.")

    type_a = _type_a_from_sweeps(spectrum)
    type_b_parts = []
    kind = result.get("kind")
    output_power_uncertainty = None
    ip3_uncertainty = None
    if kind == "two_tone":
        sides = result.get("sides", {})
        selected_side = min((name for name, side in sides.items() if side.get("resolved") and side.get("im3_dbc") is not None),
            key=lambda name: -sides[name]["im3_dbc"], default=None)
        markers = result.get("markers", {})
        if selected_side is not None:
            fundamental = "fund_" + selected_side
            product = "im3_" + selected_side
            for name in (fundamental, product):
                marker = markers.get(name)
                if marker:
                    type_b_parts.append(rsa_level_standard_uncertainty(marker["frequency_hz"]))
            raw = spectrum.get("averaging", {}).get("raw_sweeps", [])
            if len(raw) > 1:
                from .rsa import peak_near
                window = min(spectrum.get("context", {}).get("two_tone_plan", {}).get("spacing_hz", 2000) * 0.2,
                    spectrum["settings"]["rbw_hz"] * 3)
                differences = [peak_near(item, markers[product]["frequency_hz"], window)["power_dbm"]
                    - peak_near(item, markers[fundamental]["frequency_hz"], window)["power_dbm"] for item in raw]
                type_a = statistics.stdev(differences) / math.sqrt(len(differences))
        type_b = math.sqrt(sum(value ** 2 for value in type_b_parts)) if len(type_b_parts) == 2 else None
        source_u = _calibrated_source_standard_uncertainty(spectrum)
        missing = []
        if type_b is None:
            missing.append("IM3 ниже порога; неопределённость этого результата не рассчитана")
        if selected_side is None:
            type_a = None
        if len(spectrum.get("averaging", {}).get("raw_sweeps", [])) < 2:
            type_a = None
            missing.append("Повторяемость IM3: снята одна развёртка")
        if result.get("calibrated") and source_u is None:
            missing.append("Неопределённость уровня на входе DUT не найдена для оценки IIP3")
        scope = "Неопределённость IM3 относительно уровня основного тона; погрешности близких RSA-маркеров объединены без предположения об их корреляции."
        low_u = rsa_level_standard_uncertainty(markers["fund_low"]["frequency_hz"])
        high_u = rsa_level_standard_uncertainty(markers["fund_high"]["frequency_hz"])
        low_w = 10 ** ((result["fundamental_low_dbm"] - 30) / 10)
        high_w = 10 ** ((result["fundamental_high_dbm"] - 30) / 10)
        total_w = low_w + high_w
        amplitude_total = math.sqrt(low_w) + math.sqrt(high_w)
        average_weights = (low_w / total_w, high_w / total_w)
        pep_weights = (math.sqrt(low_w) / amplitude_total, math.sqrt(high_w) / amplitude_total)
        output_power_uncertainty = {
            "average_power_expanded_db_k2": 2 * math.sqrt((average_weights[0] * low_u) ** 2 + (average_weights[1] * high_u) ** 2),
            "pep_expanded_db_k2": 2 * math.sqrt((pep_weights[0] * low_u) ** 2 + (pep_weights[1] * high_u) ** 2),
            "basis": "RSA absolute-level components; input attenuator/combiner losses are not included.",
        }
        if source_u is not None:
            ip3_uncertainty = {}
            for side in ("low", "high"):
                fundamental_name = "fund_" + side
                product_name = "im3_" + side
                if result["sides"][side].get("ip3") is None:
                    continue
                side_output_u = math.sqrt(rsa_level_standard_uncertainty(markers[fundamental_name]["frequency_hz"]) ** 2
                    + rsa_level_standard_uncertainty(markers[product_name]["frequency_hz"]) ** 2)
                ip3_uncertainty[side] = 2 * math.sqrt(source_u ** 2 + (side_output_u / 2) ** 2)
    elif kind == "harmonic":
        marker = result.get("marker", {})
        band = spectrum.get("context", {}).get("harmonic_result", {})
        if band.get("fundamental_frequency_hz") is None or marker.get("frequency_hz") is None:
            return dict(status="not_available", reason="Для гармоники не сохранена частота основного тона.")
        type_b_parts = [rsa_level_standard_uncertainty(marker["frequency_hz"]),
            rsa_level_standard_uncertainty(band["fundamental_frequency_hz"])]
        type_b = math.sqrt(sum(value ** 2 for value in type_b_parts))
        source_u = None
        missing = ["Повторяемость гармонической и основной развёрток не оценена"]
        scope = "Уровень гармоники относительно основного тона; потери аттенюатора после PA в разности сокращаются."
    else:
        marker = result.get("marker", {})
        frequency = marker.get("frequency_hz")
        if frequency is None:
            return dict(status="not_available", reason="В результате нет частоты маркера.")
        type_b = rsa_level_standard_uncertainty(frequency)
        source_u = _calibrated_source_standard_uncertainty(spectrum)
        missing = []
        if source_u is None:
            missing.append("Неопределённость уровня источника на входе DUT не найдена в профиле калибровки")
        context = spectrum.get("context", {})
        if context.get("output_loss_db", 0):
            missing.append("Неопределённость ослабления тракта RSA не задана")
        scope = "RSA и сохранённые составляющие калибровки источника; неопределённости пользовательских потерь тракта не заданы."

    components = [value for value in (type_b,) if value is not None]
    if type_a is not None:
        components.append(type_a)
    if source_u is not None and kind not in ("two_tone", "harmonic"):
        components.append(source_u)
    combined = math.sqrt(sum(value ** 2 for value in components)) if components else None
    return dict(
        status="manual_based_estimate" if not missing else "partial_manual_based_estimate",
        type_a_standard_uncertainty_db=type_a,
        rsa_type_b_standard_uncertainty_db=type_b,
        source_calibration_standard_uncertainty_db=source_u,
        standard_uncertainty_db=combined,
        expanded_uncertainty_k2_db=2 * combined if combined is not None else None,
        output_power=output_power_uncertainty,
        output_power_scope=output_power_uncertainty.get("basis") if output_power_uncertainty else None,
        iip3_expanded_uncertainty_k2_db=ip3_uncertainty,
        coverage_factor=2,
        missing_components=missing,
        scope=scope,
        rsa_settings_note="Паспортные условия RSA: RBW/VBW 1 кГц, аттенюатор 10 дБ; фактические измерения могут отличаться.",
    )
