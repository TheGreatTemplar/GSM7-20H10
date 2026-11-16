"""Validated HF mixer sweep planning, before any generator output is enabled."""

from dataclasses import replace
from itertools import product
import math
import re

from .smu import SourcePlan
from .rf_methods import ISOLATION_PORTS, injection_loss, injection_port, target_frequency


def matrix_axes(settings):
    axes = []
    for key, label in (("matrix_rf_mhz", "Частоты RF"), ("matrix_rf_dbm", "Уровни RF"), ("matrix_lo_dbm", "Уровни LO")):
        try:
            values = [float(value.replace(",", ".").replace("−", "-"))
                      for value in re.split(r"[;\s]+", settings[key].strip()) if value]
        except ValueError:
            raise ValueError(f"{label}: введите числа через точку с запятой") from None
        if not values or len(values) > 100 or len(set(values)) != len(values) or not all(math.isfinite(value) for value in values):
            raise ValueError("Каждый список комплексной проверки должен содержать 1…100 разных конечных значений")
        axes.append(values)
    if math.prod(len(axis) for axis in axes) > 1000:
        raise ValueError("В комплексной проверке допускается до 1000 сочетаний за запуск")
    return axes


def sequence_count(settings, count):
    return math.prod(len(axis) for axis in matrix_axes(settings)) if settings.get("sweep_kind") == "matrix" else count


def plan_sequence(config, context, settings, count):
    count = sequence_count(settings, count)
    if type(count) is not int or not 1 <= count <= 1000:
        raise ValueError("Число измерений: 1…1000")
    kind = settings.get("sweep_kind", "fixed")
    if kind not in ("fixed", "rf_frequency", "rf_power", "lo_power", "matrix"):
        raise ValueError("Неизвестный вид серии")
    start, stop = settings.get("sweep_start", 0), settings.get("sweep_stop", 0)
    if kind not in ("fixed", "matrix") and (count < 2 or not math.isfinite(start) or not math.isfinite(stop) or start >= stop):
        raise ValueError("Для прогона нужны минимум две точки и возрастающие начало/конец")
    for key in ("rf_loss_db", "lo_loss_db", "if_loss_db", "settling_s"):
        if not math.isfinite(settings.get(key, 0)) or settings.get(key, 0) < 0:
            raise ValueError("Потери трактов и время установления должны быть конечными и неотрицательными")
    plans = []
    combinations = None
    if kind == "matrix":
        rf_frequencies, rf_levels, lo_levels = matrix_axes(settings)
        combinations = list(product(lo_levels, rf_frequencies, rf_levels))
    for index in range(count):
        value = start + (stop - start) * index / max(1, count - 1)
        point = dict(context)
        rf = value * 1e6 if kind == "rf_frequency" else context["rf_hz"]
        if combinations:
            lo_level, rf_mhz, rf_level = combinations[index]
            rf = rf_mhz * 1e6
        if not 100000 <= rf <= 60e6:
            raise ValueError("RF для стенда КВ и 50 МГц: 0,1…60 МГц")
        side = 1 if context["lo_hz"] > context["rf_hz"] else -1
        lo = rf + side * context["if_hz"]
        if not combinations:
            rf_level = value if kind == "rf_power" else settings["rf_dbm"]
            lo_level = value if kind == "lo_power" else settings["lo_dbm"]
        source_rf = context["if_hz"] if injection_port(context.get("method")) == "IF" else rf
        plan = SourcePlan(source_rf, lo, rf_level, lo_level, settings["rf_channel"], settings["lo_channel"])
        plan.validate()
        injection = (lo_level if injection_port(context["method"]) == "LO" else rf_level) - injection_loss(context, settings)
        point.update(rf_hz=rf, lo_hz=lo, sources="SMU200A", pin_dbm=injection,
                     rf_channel=plan.rf_channel, lo_channel=plan.lo_channel,
                     pin_known=settings["source_path_known"], sweep_kind=kind, series_index=index,
                     rf_set_dbm=rf_level, lo_set_dbm=lo_level,
                     rf_loss_db=settings["rf_loss_db"], lo_loss_db=settings["lo_loss_db"],
                     if_loss_db=settings.get("if_loss_db", 0))
        actual = replace(config, center_hz=target_frequency(point)) if context["method"] != "spectrum" else config
        actual.validate()
        if (point["method"] != "spectrum" and
                point["marker_window_hz"] > actual.span_hz / 2):
            raise ValueError("Окно поиска маркера шире половины обзора RSA")
        plans.append((actual, point, plan))
    return plans


def active_channels(context, plan):
    if context["method"] in ISOLATION_PORTS and injection_port(context["method"]) == "LO":
        return (plan.lo_channel,)
    if context["method"] in ISOLATION_PORTS:
        return (plan.rf_channel,)
    return (plan.lo_channel, plan.rf_channel)
