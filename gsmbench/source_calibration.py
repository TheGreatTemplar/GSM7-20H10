"""Level-dependent calibration at the combiner output, before the DUT."""

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
import threading
import uuid

from PySide6 import QtCore

from .nrp18t import Nrp18t, PowerReadingError
from .rsa import AcquisitionCancelled, AnalyzerConfig, Rsa6265, peak_near
from .rf_storage import SpectrumStore
from .smu import Smu200a, SourcePlan, verified_off
from .validation import FieldError


class NrpMeasurementError(RuntimeError):
    """Only sensor reads may be skipped; source-control failures still abort."""


def calibration_point_uncertainty(raw, plan):
    """Estimate repeatability and manual-based RSA uncertainty for one point."""
    if plan.method == "rsa":
        sample_count = len(raw)
        repeat_counts = [sample_count, sample_count]
    else:
        repeat_counts = [len(raw[index].get("readings_dbm", [])) for index in (0, 1)]
        sample_count = min(repeat_counts, default=0)

    if sample_count < 2:
        channel_u = [None, None]
        channel_sd = [None, None]
        imbalance_u = None
        imbalance_sd = None
    elif plan.method == "rsa":
        spacing = plan.f2_hz - plan.f1_hz
        window_hz = 3 * min(100, spacing / 20)
        peaks = [[], []]
        for spectrum in raw:
            for channel, frequency_hz in enumerate((plan.f1_hz, plan.f2_hz)):
                peaks[channel].append(peak_near(spectrum, frequency_hz, window_hz)["power_dbm"])
        channel_sd = [statistics.stdev(values) for values in peaks]
        channel_u = [value / math.sqrt(sample_count) for value in channel_sd]
        differences = [a - b for a, b in zip(peaks[0], peaks[1])]
        imbalance_sd = statistics.stdev(differences)
        imbalance_u = imbalance_sd / math.sqrt(sample_count)
    else:
        readings = [raw[index].get("readings_dbm", []) for index in (0, 1)]
        channel_sd = [statistics.stdev(values) if len(values) > 1 else None for values in readings]
        channel_u = [sd / math.sqrt(len(values)) if sd is not None else None
            for sd, values in zip(channel_sd, readings)]
        imbalance_sd = None
        imbalance_u = None

    analyzer = _rsa_manual_uncertainty(plan) if plan.method == "rsa" else {
        "model": None,
        "standard_uncertainty_db": None,
        "status": "not_applicable",
        "reason": "Измерения выполнены NRP18T.",
        "combined_standard_uncertainty_db": None,
        "expanded_uncertainty_k2_db": None,
    }
    if plan.method == "rsa":
        for index, component in enumerate(analyzer["channels"]):
            u_a = channel_u[index]
            u_b = component["standard_uncertainty_db"]
            component["type_a_standard_uncertainty_db"] = u_a
            component["combined_standard_uncertainty_db"] = math.sqrt(u_a ** 2 + u_b ** 2) if u_a is not None else None
            component["expanded_combined_uncertainty_k2_db"] = 2 * component["combined_standard_uncertainty_db"] if component["combined_standard_uncertainty_db"] is not None else None
    else:
        analyzer["status"] = "type_a_only"
        analyzer["reason"] = "Для NRP18T здесь рассчитана только повторяемость; Type B из документации прибора не добавлена."
        analyzer["channels"] = []
        for u_a in channel_u:
            analyzer["channels"].append({
                "standard_uncertainty_db": None,
                "combined_standard_uncertainty_db": u_a,
                "expanded_combined_uncertainty_k2_db": 2 * u_a if u_a is not None else None,
                "basis": "type_a_repeatability_only",
            })

    return {
        "basis": "type_a_repeatability_plus_manual_based_rsa_type_b_estimate" if plan.method == "rsa" else "type_a_repeatability",
        "repeat_count_by_channel": repeat_counts,
        "channels": [
            {
                "standard_deviation_db": channel_sd[index],
                "standard_uncertainty_db": channel_u[index],
                "expanded_uncertainty_k2_db": 2 * channel_u[index] if channel_u[index] is not None else None,
            }
            for index in (0, 1)
        ],
        "imbalance_a_minus_b": {
            "standard_deviation_db": imbalance_sd,
            "standard_uncertainty_db": imbalance_u,
            "expanded_uncertainty_k2_db": 2 * imbalance_u if imbalance_u is not None else None,
        },
        "analyzer": analyzer,
    }


def _rsa_manual_uncertainty(plan):
    """Use a conservative estimate from the RSA6000 manual/datasheet limits.

    Above 10 MHz, combine the 0.8 dB nominal expanded level uncertainty
    (95%, k≈2) with the rectangular 0.3 dB attenuation-switching limit.
    At/below 10 MHz, combine rectangular manual limits for absolute amplitude
    accuracy, frequency response, and attenuation switching (0.3, 0.5, 0.3 dB).
    The actual acquisition settings differ, so this is an engineering estimate,
    not a traceable uncertainty claim.
    """
    frequency_hz = (plan.f1_hz, plan.f2_hz)
    channels = []
    for frequency in frequency_hz:
        if frequency > 10e6:
            basis = "level_uncertainty_0.8db_95pct_plus_rectangular_attenuation_switching_0.3db"
            standard = math.sqrt((0.8 / 2) ** 2 + (0.3 / math.sqrt(3)) ** 2)
        else:
            basis = "rectangular_absolute_amplitude_0.3db_frequency_response_0.5db_attenuation_switching_0.3db"
            standard = math.sqrt((0.3 ** 2 + 0.5 ** 2 + 0.3 ** 2) / 3)
        channels.append({
            "frequency_hz": frequency,
            "standard_uncertainty_db": standard,
            "expanded_uncertainty_k2_db": 2 * standard,
            "basis": basis,
        })
    return {
        "model": "RIGOL RSA6265",
        "source": "RIGOL RSA6000 Data Sheet, pp. 9-11 (specification limits used as manual-derived estimates)",
        "status": "manual_based_estimate_settings_differ",
        "distribution": "mixed: published 95% uncertainty and rectangular specification limits",
        "standard_uncertainty_db": None,
        "error_envelope_db": None,
        "reason": "Серия измерена при RBW/VBW 100 Гц, аттенюаторе 20 дБ и RMS-детекторе. Для частот >10 МГц объединены паспортные 0,8 дБ при 95% (в РЭ указаны RBW/VBW 1 кГц и ослабление 10 дБ) и прямоугольная составляющая переключения аттенюатора 0,3 дБ. При частотах ≤10 МГц объединены прямоугольные пределы: абсолютная точность 0,3 дБ, АЧХ 0,5 дБ и переключение аттенюатора 0,3 дБ. Оценка не подтверждена для фактических настроек серии.",
        "channels": channels,
        "combined_standard_uncertainty_db": None,
        "expanded_uncertainty_k2_db": None,
    }


@dataclass(frozen=True)
class CalibrationPlan:
    f1_hz: float
    f2_hz: float
    loss1_db: float
    loss2_db: float
    first_dbm: float = -26
    last_dbm: float = -16
    step_db: float = 1
    tolerance_db: float = 0.1
    max_correction_db: float = 3
    measurement_loss_db: float = 0
    samples: int = 5
    iterations: int = 6
    method: str = "rsa"
    stop_frequency_hz: float = 0
    frequency_step_hz: float = 1e6

    def frequency_pairs(self):
        stop = self.stop_frequency_hz or self.f1_hz
        spacing = self.f2_hz - self.f1_hz
        if not all(math.isfinite(value) for value in (self.f1_hz, stop, spacing, self.frequency_step_hz)):
            raise FieldError("Частоты должны быть конечными", "f1_hz", "stop_frequency_hz", "frequency_step_hz")
        if not 1e5 <= self.f1_hz <= stop <= 200e6:
            raise FieldError("Частоты первого тона: 0,1…200 МГц, конечная не ниже начальной",
                "f1_hz", "stop_frequency_hz", "spacing_hz")
        if not (100 <= spacing <= 1e5 or (self.method == "nrp" and spacing == 0)):
            raise FieldError("Интервал тонов: 100 Гц…100 кГц", "spacing_hz")
        if not 0 < self.frequency_step_hz <= 200e6:
            raise FieldError("Шаг частоты должен быть положительным и не больше 200 МГц", "frequency_step_hz")
        count = math.ceil(round((stop - self.f1_hz) / self.frequency_step_hz, 10)) + 1
        if count > 201:
            raise FieldError("В серии допускается до 201 частоты; увеличьте шаг или сузьте диапазон",
                "f1_hz", "stop_frequency_hz", "frequency_step_hz")
        return [(frequency, frequency + spacing) for frequency in
            [min(stop, self.f1_hz + index * self.frequency_step_hz) for index in range(count)]]

    def levels(self):
        if not all(math.isfinite(value) for key, value in asdict(self).items() if key != "method"):
            raise ValueError("Параметры калибровки должны быть конечными")
        pairs = self.frequency_pairs()
        if not -110 <= self.first_dbm <= self.last_dbm <= 20 or not 0.1 <= self.step_db <= 20:
            raise FieldError("Проверьте диапазон мощности и положительный шаг", "first_dbm", "last_dbm", "step_db")
        if (min(self.loss1_db, self.loss2_db, self.measurement_loss_db) < 0
                or max(self.loss1_db, self.loss2_db, self.measurement_loss_db) > 100):
            raise FieldError("Ослабления должны находиться в пределах 0…100 дБ",
                "loss1_db", "loss2_db", "measurement_loss_db")
        if not 0.02 <= self.tolerance_db <= 0.5 or not 0.1 <= self.max_correction_db <= 10:
            raise FieldError("Допуск: 0,02…0,5 дБ; предел поправки: 0,1…10 дБ", "tolerance_db", "max_correction_db")
        if type(self.samples) is not int or not 1 <= self.samples <= 20 or self.iterations != 6:
            raise FieldError("Усреднение: 1…20; до шести итераций на уровень", "samples")
        if self.method not in ("rsa", "nrp"):
            raise ValueError("Неизвестный измеритель мощности")
        if self.method == "nrp" and (self.first_dbm - self.measurement_loss_db < -35
                or self.last_dbm - self.measurement_loss_db > 20):
            raise FieldError("На входе NRP18T нужен уровень −35…+20 dBm; для меньших уровней используйте RSA",
                "first_dbm", "last_dbm", "measurement_loss_db", "method")
        count = math.ceil(round((self.last_dbm - self.first_dbm) / self.step_db, 10)) + 1
        if count > 201:
            raise FieldError("До 201 уровня в профиле", "first_dbm", "last_dbm", "step_db")
        if count * len(pairs) > 10000:
            raise FieldError("В серии допускается до 10000 сочетаний частоты и мощности",
                "frequency_step_hz", "step_db", "stop_frequency_hz", "last_dbm")
        return [min(self.last_dbm, self.first_dbm + index * self.step_db) for index in range(count)]

    def source(self, target, corrections=(0, 0)):
        return SourcePlan(self.f1_hz, self.f2_hz, target + self.loss1_db + corrections[0],
            target + self.loss2_db + corrections[1], 1, 2)


def calibrated_source_plan(profile, plan, settings):
    """Exact measured grid only; never extrapolate or silently reuse another path."""
    if (profile.get("plan", {}).get("method") != "nrp"
            and not all(math.isclose(a, b, abs_tol=0.05, rel_tol=0) for a, b in
                ((profile.get("plan", {}).get("f1_hz", math.nan), plan.f1_hz),
                 (profile.get("plan", {}).get("f2_hz", math.nan), plan.f2_hz)))):
        batch = profile.get("calibration_batch_id", profile["id"])
        candidate = next((item for item in settings.get("power_calibration_profiles", [])
            if item.get("calibration_batch_id", item["id"]) == batch
            and item.get("plan", {}).get("method") == profile.get("plan", {}).get("method")
            and all(math.isclose(a, b, abs_tol=0.05, rel_tol=0) for a, b in
                ((item.get("plan", {}).get("f1_hz", math.nan), plan.f1_hz),
                 (item.get("plan", {}).get("f2_hz", math.nan), plan.f2_hz)))), None)
        if candidate is not None:
            profile = candidate
    if (profile.get("schema") != 1 or profile.get("state") not in ("complete", "complete_with_gaps") or not profile.get("rf_off_confirmed")
            or profile.get("demo") != settings["demo"] or not profile.get("points")):
        raise FieldError("Нужен завершённый профиль калибровки в текущем режиме, с подтверждённым RF OFF", "profiles")
    config = CalibrationPlan(**profile["plan"])
    grid = config.levels()
    recorded = sorted(point["target_dbm"] for point in profile["points"] + profile.get("skipped", []))
    if len(recorded) != len(grid) or any(not math.isclose(actual, target, rel_tol=0, abs_tol=1e-6)
            for actual, target in zip(recorded, grid)):
        raise ValueError("Профиль содержит неполную или повреждённую сетку уровней")
    if (profile["path"] != settings["calibration_path"] or profile["smu_host"] != settings["smu_host"]
            or int(profile["smu_port"]) != int(settings["smu_port"])):
        raise FieldError("Профиль калибровки относится к другому тракту или адресу SMU", "path", "smu_host", "smu_port", "profiles")
    if (config.method == "nrp" and config.f1_hz == config.f2_hz
            and not settings.get("single_frequency_lookup")):
        return calibrated_nrp_plan(profile, plan, settings)
    if not all(math.isclose(a, b, abs_tol=0.05, rel_tol=0) for a, b in
            ((config.f1_hz, plan.f1_hz), (config.f2_hz, plan.f2_hz))):
        raise FieldError("Для этой пары частот нет калибровки; выполните отдельную процедуру", "f1_mhz", "spacing_hz", "profiles")
    if not all(math.isclose(a, b, abs_tol=1e-6, rel_tol=0) for a, b in
            ((config.loss1_db, plan.loss1_db), (config.loss2_db, plan.loss2_db))):
        raise FieldError("Ослабления вводов изменены после калибровки", "loss1", "loss2", "profiles")
    commands = []
    for channel, target in enumerate((plan.level1_dbm - plan.loss1_db, plan.level2_dbm - plan.loss2_db)):
        matches = [point for point in profile["points"] if math.isclose(point["target_dbm"], target, abs_tol=1e-6, rel_tol=0)]
        if len(matches) != 1:
            raise FieldError(f"Нет калибровки для {target:+g} dBm на входе DUT. Интерполяция и экстраполяция отключены.",
                "level1", "level2", "drive_stop", "profiles")
        point = matches[0]
        if abs(point["measured_dbm"][0] - point["measured_dbm"][1]) > config.tolerance_db:
            raise ValueError("Разность тонов в профиле превышает допуск")
        measured = point["measured_dbm"][channel]
        command = point["commands_dbm"][channel]
        if (not all(math.isfinite(value) for value in (measured, command))
                or abs(measured - target) > config.tolerance_db
                or abs(command - (target + (plan.loss1_db, plan.loss2_db)[channel])) > config.max_correction_db + 1e-6):
            raise ValueError("Недостоверная точка калибровки")
        commands.append(command)
    result = SourcePlan(plan.f1_hz, plan.f2_hz, *commands, 1, 2)
    result.validate()
    return result


def calibrated_nrp_plan(selected, plan, settings):
    """NRP profiles hold both channels at one frequency; look up each tone separately."""
    batch = selected.get("calibration_batch_id", selected["id"])
    available = settings.get("power_calibration_profiles", [selected])
    commands = []
    for channel, frequency in enumerate((plan.f1_hz, plan.f2_hz)):
        profile = next((item for item in available
            if item.get("calibration_batch_id", item["id"]) == batch
            and item["plan"]["method"] == "nrp"
            and math.isclose(item["plan"]["f1_hz"], frequency, abs_tol=0.05, rel_tol=0)
            and math.isclose(item["plan"]["f2_hz"], frequency, abs_tol=0.05, rel_tol=0)), None)
        if profile is None:
            raise FieldError(f"В серии NRP нет калибровки канала {('A', 'B')[channel]} на {frequency / 1e6:g} МГц. "
                "Добавьте эту частоту в калибровочную сетку; соседняя частота не подставляется.", "profiles")
        target = (plan.level1_dbm - plan.loss1_db, plan.level2_dbm - plan.loss2_db)[channel]
        single = replace(plan, f1_hz=frequency, spacing_hz=0,
            level1_dbm=target + plan.loss1_db, level2_dbm=target + plan.loss2_db)
        source = calibrated_source_plan(profile, single, dict(settings, single_frequency_lookup=True))
        commands.append((source.rf_dbm, source.lo_dbm)[channel])
    result = SourcePlan(plan.f1_hz, plan.f2_hz, *commands, 1, 2)
    result.validate()
    return result


class PowerCalibrationWorker(QtCore.QThread):
    log = QtCore.Signal(str)
    status = QtCore.Signal(str)
    failed = QtCore.Signal(str)
    outputs = QtCore.Signal(object)
    profile_ready = QtCore.Signal(object)
    field_failed = QtCore.Signal(object)

    def __init__(self, owner, settings, plan):
        super().__init__(owner)
        self.settings, self.plan = settings, plan
        self.operation = "calibrate"
        self.stop = threading.Event()
        self.enable_attempted = False
        self.result_state = None
        self.summary_text = None
        self.final_profile = None

    def run(self):
        try:
            self.plan.levels()
            pairs = self.plan.frequency_pairs()
        except Exception as error:
            self.field_failed.emit(error)
            self.failed.emit(str(error))
            self.result_state = "failed"
            self.outputs.emit(dict(known=False, active=False, requires_off=False))
            return
        if len(pairs) == 1:
            self.run_single()
            return
        batch = str(uuid.uuid4())
        self.result_state = "complete"
        completed, measured, skipped = 0, 0, 0
        for index, (first, second) in enumerate(pairs):
            if self.stop.is_set():
                self.result_state = "cancelled"
                break
            settings = dict(self.settings, calibration_batch_id=batch,
                frequency_sweep=dict(first_hz=self.plan.f1_hz, last_hz=pairs[-1][0],
                    step_hz=self.plan.frequency_step_hz, count=len(pairs)))
            plan = replace(self.plan, f1_hz=first, f2_hz=second, stop_frequency_hz=0)
            child = PowerCalibrationWorker(None, settings, plan)
            child.stop = self.stop
            child.log.connect(self.log.emit)
            child.status.connect(lambda text, index=index, first=first: self.status.emit(
                f"Частота {index + 1}/{len(pairs)} · {first / 1e6:g} МГц · {text}"))
            child.failed.connect(self.failed.emit)
            child.field_failed.connect(self.field_failed.emit)
            child.outputs.connect(self.outputs.emit)
            child.profile_ready.connect(self.profile_ready.emit)
            child.run_single()
            if child.final_profile:
                measured += len(child.final_profile["points"])
                skipped += len(child.final_profile.get("skipped", []))
            if child.result_state not in ("complete", "complete_with_gaps"):
                self.result_state = child.result_state
                break
            completed += 1
            if child.result_state == "complete_with_gaps":
                self.result_state = "complete_with_gaps"
        state = {"complete": "завершена", "complete_with_gaps": "завершена с пропусками",
            "cancelled": "остановлена", "failed": "ОШИБКА"}.get(self.result_state, self.result_state)
        self.summary_text = (f"Серия: {state} · частот {completed}/{len(pairs)} · "
            f"принято уровней {measured}, пропущено {skipped}. "
            + ("RF OFF подтверждён." if self.result_state in ("complete", "complete_with_gaps", "cancelled") else
                "Проверьте сообщение об ошибке и состояние RF OFF."))
        self.status.emit(self.summary_text)

    def run_single(self):
        settings, plan = self.settings, self.plan
        smu = meter = None
        profile = dict(schema=1, id=str(uuid.uuid4()), created_at=datetime.now(timezone.utc).isoformat(),
            demo=settings["demo"], path=settings["calibration_path"], plan=asdict(plan),
            smu_host=settings["smu_host"], smu_port=settings["smu_port"], points=[], state="running",
            rf_off_confirmed=False, attempts=[], skipped=[], basis="combiner_output_before_DUT")
        profile.update(calibration_batch_id=settings.get("calibration_batch_id", profile["id"]),
            frequency_sweep=settings.get("frequency_sweep", dict(first_hz=plan.f1_hz, last_hz=plan.f1_hz,
                step_hz=plan.frequency_step_hz, count=1)))
        profile["uncertainty_model"] = {
            "repeatability": "Type A from repeated readings; uA=s/sqrt(n); expanded UA=2*uA (k=2).",
            "analyzer_model": "RIGOL RSA6265" if plan.method == "rsa" else None,
            "analyzer_error": "Manual-based, frequency-dependent Type B estimate combined per tone. Actual RBW/VBW, attenuation, and detector settings differ from published conditions." if plan.method == "rsa" else "NRP18T Type B uncertainty is not specified by this model.",
            "analyzer_source": "RIGOL RSA6000 Data Sheet, pp. 9-11",
            "combined_uncertainty": "u_c=sqrt(u_A^2+u_B^2); U_c=2*u_c (k=2).",
            "expanded_combined_uncertainty_k2": "Stored per channel and point.",
        }
        try:
            levels = plan.levels()
            unavailable = {}
            if not settings["demo"]:
                smu = Smu200a(settings["smu_host"], settings["smu_port"], self.log.emit)
                smu.prepare(SourcePlan(plan.f1_hz, plan.f2_hz, -30, -30, 1, 2))
                profile["generator_identity"] = smu.identity
                for target in levels:
                    try:
                        smu.validate_plan(plan.source(target))
                    except ValueError as error:
                        unavailable[target] = str(error)
                if plan.method == "rsa":
                    meter = Rsa6265(settings["rsa_host"], self.log.emit)
                else:
                    meter = Nrp18t(settings["nrp_resource"], self.log.emit)
                    self.status.emit("NRP18T: установка нуля при подтверждённых RF OFF обоих каналов SMU")
                    profile["meter_settings"] = meter.prepare(self.stop, settings.get("nrp_averages", 128))
                profile["meter_identity"] = meter.identity
            else:
                profile.update(generator_identity="SIMULATOR,SMU200A", meter_identity="SIMULATOR," + plan.method)
            profile["meter_endpoint"] = settings["rsa_host"] if plan.method == "rsa" else settings["nrp_resource"]
            for index, target in enumerate(levels):
                if self.stop.is_set():
                    raise AcquisitionCancelled()
                if target in unavailable:
                    self.skip_level(profile, target, unavailable[target], plan.source(target), 0, smu, kind="source_limit")
                    continue
                corrections = [0.0, 0.0]
                skip_level = False
                for iteration in range(plan.iterations):
                    if self.stop.is_set():
                        raise AcquisitionCancelled()
                    source = plan.source(target, corrections)
                    if max(source.rf_dbm, source.lo_dbm) > 20:
                        self.skip_level(profile, target, "Требуется команда SMU выше +20 dBm с поправкой",
                            source, iteration + 1, smu, kind="source_limit")
                        skip_level = True
                        break
                    source.validate()
                    if smu:
                        try:
                            smu.validate_plan(source)
                        except ValueError as error:
                            self.skip_level(profile, target, str(error), source, iteration + 1, smu, kind="source_limit")
                            skip_level = True
                            break
                        smu.all_off()
                        smu.setpoint(source)
                    self.status.emit(f"Калибровка {index + 1}/{len(levels)} · {target:+g} dBm/тон · итерация {iteration + 1}")
                    try:
                        measured, raw = self.measure(meter, smu, source)
                    except (AcquisitionCancelled, InterruptedError):
                        raise
                    except Exception as error:
                        if plan.method != "nrp" or not isinstance(error, NrpMeasurementError):
                            raise
                        if smu:
                            verified_off(smu)
                        profile["skipped"].append(dict(target_dbm=target, error=str(error), iteration=iteration + 1,
                            commands_dbm=[source.rf_dbm, source.lo_dbm], kind="measurement_error"))
                        self.log.emit(f"ПРОПУСК NRP18T · {target:+g} dBm: {error}")
                        self.status.emit(f"ОШИБКА NRP18T на {target:+g} dBm: пропуск уровня; {error}")
                        if meter and not isinstance(error.__cause__, PowerReadingError):
                            # Never reuse a session with an unread/stale reply after a timeout.
                            meter.close()
                            meter = None
                            try:
                                meter = Nrp18t(settings["nrp_resource"], self.log.emit)
                                profile["meter_settings"] = meter.prepare(self.stop, settings.get("nrp_averages", 128))
                            except (AcquisitionCancelled, InterruptedError):
                                raise
                            except Exception as reconnect_error:
                                if meter:
                                    meter.close()
                                    meter = None
                                raise ConnectionError("NRP18T: не удалось восстановить связь при RF OFF: " + str(reconnect_error)) from reconnect_error
                        self.save(profile)
                        self.profile_ready.emit(json.loads(json.dumps(profile)))
                        skip_level = True
                        break
                    profile["attempts"].append(dict(target_dbm=target, iteration=iteration + 1,
                        commands_dbm=[source.rf_dbm, source.lo_dbm], measured_dbm=measured, raw=raw))
                    errors = [target - value for value in measured]
                    # Require each absolute level AND the difference to meet the tolerance.
                    if max(map(abs, errors)) <= plan.tolerance_db and abs(measured[0] - measured[1]) <= plan.tolerance_db:
                        profile["points"].append(dict(target_dbm=target, measured_dbm=measured,
                            commands_dbm=[source.rf_dbm, source.lo_dbm], correction_db=corrections,
                            imbalance_db=measured[0] - measured[1], iterations=iteration + 1,
                            uncertainty=calibration_point_uncertainty(raw, plan)))
                        self.save(profile)
                        self.profile_ready.emit(json.loads(json.dumps(profile)))
                        break
                    corrections = [round(value + error, 3) for value, error in zip(corrections, errors)]
                    if any(abs(value) > plan.max_correction_db for value in corrections):
                        self.skip_level(profile, target, "Поправка превысила заданный предел",
                            source, iteration + 1, smu, measured, kind="correction_limit")
                        skip_level = True
                        break
                else:
                    self.skip_level(profile, target, "Уровни не сошлись за шесть итераций",
                        source, plan.iterations, smu, measured, kind="nonconvergence")
                    skip_level = True
                if skip_level:
                    continue
            profile["state"] = "complete_with_gaps" if profile["skipped"] else "complete"
        except (AcquisitionCancelled, InterruptedError):
            profile["state"] = "cancelled"
            self.status.emit("Калибровка остановлена")
        except Exception as error:
            profile.update(state="failed", error=str(error))
            self.field_failed.emit(error)
            self.failed.emit(str(error))
        finally:
            off_error = None
            if smu:
                try:
                    verified_off(smu)
                except Exception as error:
                    off_error = str(error)
            elif not settings["demo"]:
                off_error = "Связь с SMU не установлена"
            profile["rf_off_confirmed"] = off_error is None
            self.outputs.emit(dict(known=off_error is None, active=bool(off_error and self.enable_attempted),
                requires_off=bool(off_error and self.enable_attempted)))
            if off_error:
                profile.update(state="failed", off_error=off_error)
                self.failed.emit("RF OFF не подтверждён: " + off_error)
            try:
                self.save(profile)
                self.result_state = profile["state"]
                self.final_profile = profile
                self.profile_ready.emit(profile)
                if profile["state"] in ("complete", "complete_with_gaps"):
                    self.status.emit(f"Калибровка сохранена: измерено {len(profile['points'])}, пропущено {len(profile['skipped'])}. "
                        "Оба выхода SMU OFF. Верните DUT и выберите профиль для измерений.")
            except Exception as error:
                self.result_state = "failed"
                self.failed.emit("Сохранение профиля: " + str(error))
            for driver in (meter, smu):
                if driver:
                    try:
                        driver.close()
                    except Exception as error:
                        self.failed.emit("Закрытие связи: " + str(error))

    def skip_level(self, profile, target, reason, source, iteration, smu, measured=None, kind="measurement_error"):
        if smu:
            verified_off(smu)
        point = dict(target_dbm=target, error=reason, iteration=iteration,
            commands_dbm=[source.rf_dbm, source.lo_dbm], kind=kind)
        if measured is not None:
            point.update(measured_dbm=measured, imbalance_db=measured[0] - measured[1])
        profile["skipped"].append(point)
        self.log.emit(f"ПРОПУСК · {target:+g} dBm: {reason}")
        self.status.emit(f"ПРОПУСК · {target:+g} dBm: {reason}; переход к следующему уровню")
        self.save(profile)
        self.profile_ready.emit(json.loads(json.dumps(profile)))

    def measure(self, meter, smu, source):
        plan = self.plan
        raw, measured = [], []
        if plan.method == "rsa":
            spacing = plan.f2_hz - plan.f1_hz
            config = AnalyzerConfig(center_hz=(plan.f1_hz + plan.f2_hz) / 2, span_hz=5 * spacing,
                rbw_hz=min(100, spacing / 20), vbw_hz=min(100, spacing / 20), detector="RMS",
                points=1601, attenuation_db=self.settings["rsa_attenuation"], reference_dbm=self.settings["rsa_reference"])
            actual = meter.configure(config) if meter else None
            self.enable_attempted = smu is not None
            expected = smu.enable((1, 2), self.stop) if smu else None
            if self.stop.wait(self.settings["settling_s"]):
                raise AcquisitionCancelled()
            values = [[], []]
            for _ in range(plan.samples):
                if self.stop.is_set():
                    raise AcquisitionCancelled()
                if meter:
                    spectrum = meter.acquire(self.stop)
                    if spectrum["settings"] != actual or spectrum.get("if_overload"):
                        raise ValueError("Настройки RSA изменены или перегрузка ПЧ")
                    if max(spectrum["power_dbm"]) >= actual["reference_dbm"] - 0.5:
                        raise ValueError("Недостаточный запас по Ref Level RSA")
                    for i, hz in enumerate((plan.f1_hz, plan.f2_hz)):
                        peak = peak_near(spectrum, hz, config.rbw_hz * 3)["power_dbm"]
                        noise = statistics.median(spectrum["power_dbm"])
                        if peak - noise < 20:
                            raise ValueError("RSA: тон менее чем на 20 дБ выше шумового фона")
                        values[i].append(peak)
                    raw.append(spectrum)
                    if smu.state() != expected:
                        raise ValueError("SMU изменился во время калибровки")
                else:
                    for i, command in enumerate((source.rf_dbm, source.lo_dbm)):
                        values[i].append(command - (plan.loss1_db, plan.loss2_db)[i] + (0.23, -0.17)[i] - plan.measurement_loss_db)
            measured = [10 * math.log10(statistics.mean(10 ** (value / 10) for value in group))
                + plan.measurement_loss_db for group in values]
        else:
            for i, hz in enumerate((plan.f1_hz, plan.f2_hz)):
                if self.stop.is_set():
                    raise AcquisitionCancelled()
                if smu:
                    smu.all_off()
                    self.enable_attempted = True
                    expected = smu.enable((i + 1,), self.stop)
                if self.stop.wait(self.settings.get("nrp_settling_s", 1)):
                    raise AcquisitionCancelled()
                transition = None
                if meter:
                    try:
                        transition = meter.start_level(hz, self.stop)
                    except (AcquisitionCancelled, InterruptedError):
                        raise
                    except Exception as error:
                        raise NrpMeasurementError(str(error)) from error
                values = []
                for _ in range(plan.samples):
                    if meter:
                        try:
                            value = meter.read_dbm(hz, self.stop)
                        except (AcquisitionCancelled, InterruptedError):
                            raise
                        except Exception as error:
                            raise NrpMeasurementError(str(error)) from error
                    else:
                        value = ((source.rf_dbm, source.lo_dbm)[i] - (plan.loss1_db, plan.loss2_db)[i]
                            + (0.23, -0.17)[i] - plan.measurement_loss_db)
                    values.append(value)
                    if smu and smu.state() != expected:
                        raise ValueError("Состояние SMU изменилось при измерении NRP18T")
                measured.append(10 * math.log10(statistics.mean(10 ** (value / 10) for value in values)) + plan.measurement_loss_db)
                raw.append(dict(channel=i + 1, frequency_hz=hz, readings_dbm=values, transition=transition))
        return measured, raw

    def save(self, profile):
        directory = Path(self.settings["root"]) / "data" / "smu-calibrations"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (profile["id"] + ".json")
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(profile, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        temporary.replace(target)
        if self.settings.get("database"):
            SpectrumStore(self.settings["database"], profile["demo"]).save_calibration(profile)


def load_profiles(root, demo, include_incomplete=False):
    profiles = []
    for path in (Path(root) / "data" / "smu-calibrations").glob("*.json"):
        try:
            profile = json.loads(path.read_text(encoding="utf-8"))
            usable = profile.get("state") in ("complete", "complete_with_gaps") and profile.get("rf_off_confirmed") and profile.get("points")
            if (profile.get("demo") == demo and (usable or
                    (include_incomplete and (profile.get("points") or profile.get("skipped"))))):
                profiles.append(profile)
        except (OSError, ValueError):
            continue
    return sorted(profiles, key=lambda item: item["created_at"], reverse=True)
