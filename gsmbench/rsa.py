"""RSA6265 swept-spectrum driver and preliminary mixer calculations."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math
import copy
import statistics
import time
import uuid

from .mixer import PathLoss, conversion_loss, isolation
from .rf_instruments import connection_error_message, lan_resource
from .rf_methods import ISOLATION_PORTS, target_frequency
from .validation import FieldError


class AcquisitionCancelled(Exception):
    pass


@dataclass(frozen=True)
class AnalyzerConfig:
    center_hz: float = 11059200
    span_hz: float = 200000
    rbw_hz: float = 1000
    vbw_hz: float = 1000
    attenuation_db: int = 10
    reference_dbm: float = 0
    points: int = 801
    detector: str = "POS"

    def validate(self):
        if not all(math.isfinite(value) for value in
                   (self.center_hz, self.span_hz, self.rbw_hz, self.vbw_hz, self.reference_dbm)):
            raise ValueError("Настройки RSA должны быть конечными числами")
        if self.span_hz <= 0 or self.center_hz - self.span_hz / 2 < 0 or self.center_hz + self.span_hz / 2 > 26.5e9:
            raise FieldError("Развёртка RSA должна находиться в пределах 0…26,5 ГГц и иметь ненулевую полосу", "center_mhz", "span_mhz")
        if not 1 <= self.rbw_hz <= 1e7 or not 1 <= self.vbw_hz <= 1e7:
            raise FieldError("RBW и VBW должны быть в пределах 1 Гц…10 МГц", "rbw_hz", "vbw_hz")
        if type(self.points) is not int or not 101 <= self.points <= 10001:
            raise FieldError("Число точек RSA: 101…10001", "points")
        if type(self.attenuation_db) is not int or not 0 <= self.attenuation_db <= 50:
            raise FieldError("Аттенюатор: 0…50 дБ", "attenuation_db")
        if not -170 <= self.reference_dbm <= 30 or self.detector not in ("POS", "NORM", "RMS", "SAMP"):
            raise FieldError("Недопустимый опорный уровень или детектор RSA", "reference_dbm", "detector")


def parse_trace(text):
    if text.lstrip().startswith("#"):
        raise ValueError("RSA вернул двоичный блок вместо ASCII; снимок не принят")
    values = [float(value) for value in text.strip().split(",")]
    if not 101 <= len(values) <= 100001 or any(not math.isfinite(value) or abs(value) > 1000 for value in values):
        raise ValueError("Повреждённая или неполная трасса RSA")
    return values


def frequencies(spectrum):
    values = spectrum["power_dbm"]
    start, stop = spectrum["settings"]["start_hz"], spectrum["settings"]["stop_hz"]
    return [start + index * (stop - start) / (len(values) - 1) for index in range(len(values))]


def peak_near(spectrum, target_hz, window_hz):
    """A trace-bin peak, not an interpolated marker or an integrated channel power."""
    if not math.isfinite(target_hz) or not math.isfinite(window_hz) or window_hz <= 0:
        raise ValueError("Нужны конечная частота маркера и положительная ширина поиска")
    bins = frequencies(spectrum)
    if target_hz - window_hz < bins[0] or target_hz + window_hz > bins[-1]:
        raise ValueError("Окно поиска маркера не помещается в считанный спектр")
    candidates = [index for index, hz in enumerate(bins) if abs(hz - target_hz) <= window_hz]
    if not candidates:
        raise ValueError("В окне маркера нет точек: уменьшите полосу обзора или увеличьте число точек")
    index = max(candidates, key=lambda index: spectrum["power_dbm"][index])
    return dict(frequency_hz=bins[index], power_dbm=spectrum["power_dbm"][index], index=index)


def average_spectra(spectra, context):
    if not spectra:
        raise ValueError("Нет развёрток для усреднения")
    if len(spectra) == 1:
        return spectra[0]
    first = spectra[0]
    for spectrum in spectra:
        if (spectrum["settings"] != first["settings"] or spectrum["identity"] != first["identity"]
                or spectrum["demo"] != first["demo"] or len(spectrum["power_dbm"]) != len(first["power_dbm"])):
            raise ValueError("Нельзя усреднять разные частотные сетки, приборы или настройки RSA")
        if spectrum.get("if_overload"):
            raise ValueError("Перегруженная развёртка не принимается в среднее")
    result = copy.deepcopy(first)
    result["id"] = str(uuid.uuid4())
    result["measured_at"] = spectra[-1]["measured_at"]
    # Average linear power, never arithmetic dBm or neighboring frequencies.
    powers = []
    for values in zip(*(spectrum["power_dbm"] for spectrum in spectra)):
        reference = max(values)
        powers.append(reference + 10 * math.log10(statistics.mean(10 ** ((value - reference) / 10) for value in values)))
    result["power_dbm"] = powers
    peaks = [max(spectrum["power_dbm"]) if context.get("method", "spectrum") == "spectrum" else
        peak_near(spectrum, target_frequency(context), context["marker_window_hz"])["power_dbm"] for spectrum in spectra]
    result["acquisition"] = "independent_sweeps_power_average"
    result["averaging"] = dict(count=len(spectra), method="linear_power", marker_std_db=statistics.stdev(peaks),
        marker_span_db=max(peaks) - min(peaks), marker_values_dbm=peaks, raw_sweeps=spectra)
    return result


def mixer_result(spectrum):
    """Persist a reproducible, explicitly preliminary result with each spectrum."""
    context = spectrum.get("context", {})
    method = context.get("method", "spectrum")
    if method in ("two_tone_pa", "two_tone_mixer"):
        from .two_tone import analyze_two_tone
        return analyze_two_tone(spectrum)
    if method == "harmonic_pa":
        band = context.get("harmonic_result", {})
        if spectrum.get("if_overload") or not all(key in band for key in ("dbc", "frequency_hz", "rsa_dbm")):
            return dict(kind="harmonic", status="not_calculated", reason="Неполный гармонический результат либо перегрузка")
        return dict(kind="harmonic", status="preliminary", value_db=-band["dbc"] if band["dbc"] is not None else None,
                    marker=dict(frequency_hz=band["frequency_hz"], power_dbm=band["rsa_dbm"]))
    if method == "spectrum":
        return dict(status="spectrum_only")
    try:
        if method != "conversion" and method not in ISOLATION_PORTS:
            raise ValueError("Неизвестная методика")
        if spectrum.get("if_overload", False):
            raise ValueError("RSA сообщил о перегрузке ПЧ")
        if not context.get("pin_known") or not context.get("path_known"):
            missing = []
            if not context.get("pin_known"):
                missing.append("уровень на входе смесителя")
            if not context.get("path_known"):
                missing.append("ослабление тракта до RSA")
            raise ValueError("Не подтверждено: " + "; ".join(missing) + ". Исходный спектр сохранён.")
        metadata = spectrum["settings"]
        if metadata["amplitude_correction"] or metadata["reference_offset_db"] or metadata["external_gain_db"]:
            raise ValueError("На RSA включены амплитудные поправки: исключите двойной учёт до расчёта")
        if metadata["trace_type"] != "WRIT":
            raise ValueError("Нужна трасса Clear/Write: накопленная трасса не является новой точкой измерения")
        step = (metadata["stop_hz"] - metadata["start_hz"]) / (len(spectrum["power_dbm"]) - 1)
        if step > metadata["rbw_hz"] / 2:
            raise ValueError("Слишком редкая сетка относительно RBW: уменьшите обзор или увеличьте число точек")
        target = target_frequency(context)
        marker = peak_near(spectrum, target, context["marker_window_hz"])
        if marker["power_dbm"] >= metadata["reference_dbm"]:
            raise ValueError("Пик достигает опорного уровня; проверьте перегрузку RSA")
        calculate = conversion_loss if method == "conversion" else isolation
        value = calculate(context["pin_dbm"], marker["power_dbm"], PathLoss(0, context["output_loss_db"]))
        return dict(status="preliminary", method=method, value_db=value, marker=marker,
                    reason="Шумовой пол, перегрузка и мощности на портах смесителя автоматически не подтверждены")
    except (ValueError, KeyError, TypeError) as error:
        return dict(status="not_calculated", reason=str(error))


class Rsa6265:
    METADATA = {
        "mode": (":INST:SEL?", str),
        "unit": (":UNIT:POW?", str),
        "start_hz": (":FREQ:STAR?", float),
        "stop_hz": (":FREQ:STOP?", float),
        "rbw_hz": (":BAND:RES?", float),
        "vbw_hz": (":BAND:VID?", float),
        "points": (":SWE:POIN?", int),
        "trace_type": (":TRAC1:TYPE?", str),
        "detector": (":DET:TRAC1?", str),
        "reference_dbm": (":DISP:WIND1:TRAC:Y:SCAL:RLEV?", float),
        "attenuation_db": (":POW:RF:ATT?", float),
        "preamp": (":POW:RF:GAIN:STAT?", int),
        "reference_offset_db": (":DISP:WIND1:TRAC:Y:SCAL:RLEV:OFFS?", float),
        "external_gain_db": (":CORR:SA:RF:GAIN?", float),
        "amplitude_correction": (":CORR:CSET:ALL:STAT?", int),
    }

    def __init__(self, host, log=lambda text: None, manager_factory=None):
        if manager_factory is None:
            import pyvisa
            manager_factory = pyvisa.ResourceManager
        self.log = log
        self.manager = manager_factory("@py")
        self.device = None
        try:
            self.device = self.manager.open_resource(lan_resource(host), open_timeout=3000)
            self.device.timeout = 3000
            self.device.write_termination = "\n"
            self.device.read_termination = "\n"
            self.identity = self.query("*IDN?")
            fields = self.identity.split(",")
            if len(fields) < 2 or fields[1].strip().upper() != "RSA6265":
                raise ValueError("Ожидался RSA6265: " + self.identity)
        except Exception as error:
            self.close()
            message = connection_error_message("RSA6265", f"{host} / VXI-11", error)
            if message:
                raise ConnectionError(message) from error
            raise

    def write(self, command):
        self.log("> " + command)
        self.device.write(command)

    def query(self, command):
        self.log("> " + command)
        response = self.device.query(command).strip()
        self.log("< " + (response if len(response) < 300 else f"[трасса ASCII: {len(response)} символов]"))
        return response

    def check_error(self):
        response = self.query(":SYST:ERR?")
        if int(response.split(",", 1)[0]) != 0:
            raise RuntimeError("RSA: " + response)

    def metadata(self):
        values = self.query(";".join(command for command, _ in self.METADATA.values())).split(";")
        if len(values) != len(self.METADATA):
            raise ValueError("Неполный пакет параметров RSA")
        settings = {name: cast(value.strip()) for (name, (_, cast)), value in zip(self.METADATA.items(), values)}
        settings["detector"] = {"RAV": "RMS", "RAVERAGE": "RMS"}.get(settings["detector"], settings["detector"])
        if settings["mode"] != "SA" or settings["unit"] != "DBM":
            raise ValueError("Для спектра нужны режим SA и единицы dBm. Примените настройки RSA.")
        numeric = [value for value in settings.values() if isinstance(value, (int, float))]
        if not all(math.isfinite(value) for value in numeric) or settings["stop_hz"] <= settings["start_hz"]:
            raise ValueError("Недостоверные настройки развёртки RSA")
        return settings

    def configure(self, config):
        config.validate()
        self.check_error()
        commands = [":INST:SEL SA", ":UNIT:POW DBM", ":POW:RF:GAIN:STAT OFF",
                    f":FREQ:CENT {config.center_hz:.12g}", f":FREQ:SPAN {config.span_hz:.12g}",
                    ":BAND:RES:AUTO OFF", f":BAND:RES {config.rbw_hz:.12g}",
                    ":BAND:VID:AUTO OFF", f":BAND:VID {config.vbw_hz:.12g}",
                    ":POW:RF:ATT:AUTO OFF", f":POW:RF:ATT {config.attenuation_db}",
                    f":DISP:WIND1:TRAC:Y:SCAL:RLEV {config.reference_dbm:.12g}",
                    f":SWE:POIN {config.points}", ":DET:TRAC1:AUTO OFF",
                    f":DET:TRAC1 {config.detector}", ":TRAC1:TYPE WRIT"]
        for command in commands:
            self.write(command)
        self.check_error()
        actual = self.metadata()
        if not math.isclose(actual["reference_dbm"], config.reference_dbm, abs_tol=0.05) or not math.isclose(
                actual["attenuation_db"], config.attenuation_db, abs_tol=0.05):
            raise ValueError(f"RSA применил Ref {actual['reference_dbm']:g} dBm / Att {actual['attenuation_db']:g} дБ "
                             f"вместо Ref {config.reference_dbm:g} / Att {config.attenuation_db:g}. "
                             "Увеличьте аттенюатор для высокого опорного уровня.")
        return actual

    def read_current(self):
        before = self.metadata()
        powers = parse_trace(self.query(":TRAC:DATA? TRACE1"))
        after = self.metadata()
        if before != after or len(powers) != before["points"]:
            raise ValueError("Настройки RSA изменились при чтении или трасса неполная; повторите измерение")
        self.check_error()
        overloaded = int(self.query(":INP:IF:OVER?"))
        if overloaded not in (0, 1):
            raise ValueError("Недостоверный признак перегрузки RSA")
        return dict(id=str(uuid.uuid4()), measured_at=datetime.now(timezone.utc).isoformat(),
                    identity=self.identity, demo=False, settings=before, power_dbm=powers,
                    acquisition="current_trace", if_overload=bool(overloaded),
                    warnings=["Шумовой предел и перегрузка RF-тракта автоматически не подтверждены"])

    def acquire(self, stop, timeout=120):
        if stop.is_set():
            raise AcquisitionCancelled()
        continuous = int(self.query(":INIT:CONT?"))
        try:
            self.write(":ABOR")
            self.write(":INIT:CONT OFF")
            self.query("*ESR?")
            self.write(":INIT")
            self.write("*OPC")
            deadline = time.monotonic() + timeout
            while True:
                if stop.is_set():
                    raise AcquisitionCancelled()
                status = int(self.query("*ESR?"))
                if status & 0x3c:
                    raise RuntimeError(f"Ошибка развёртки RSA, ESR={status}")
                if status & 1:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"RSA не завершил развёртку за {timeout:g} с")
                stop.wait(0.1)
            spectrum = self.read_current()
            spectrum["acquisition"] = "single_sweep"
            return spectrum
        finally:
            self.write(":ABOR")
            self.write(f":INIT:CONT {continuous}")
            if int(self.query(":INIT:CONT?")) != continuous:
                raise RuntimeError("RSA не подтвердил восстановление режима развёртки")

    def close(self):
        try:
            if self.device is not None:
                try:
                    session = self.manager.visalib.sessions[self.device.session]
                    error = session.interface.device_local(session.link, 0, session.lock_timeout, session._io_timeout)
                    if error:
                        self.log(f"RSA: возврат Local, код VXI11 {error}")
                except Exception as error:
                    self.log("RSA: возврат Local не подтверждён: " + str(error))
                self.device.close()
        finally:
            self.manager.close()


def demo_spectrum(config):
    config.validate()
    settings = dict(start_hz=config.center_hz - config.span_hz / 2,
                    stop_hz=config.center_hz + config.span_hz / 2, mode="SA", unit="DBM",
                    trace_type="WRIT", preamp=0, reference_offset_db=0,
                    external_gain_db=0, amplitude_correction=0, **asdict(config))
    width = max(config.rbw_hz, config.span_hz / config.points)
    powers = []
    for index in range(config.points):
        hz = settings["start_hz"] + config.span_hz * index / (config.points - 1)
        noise = -105 + 2 * math.sin(index * 1.731)
        tone = -27 - 12 * ((hz - config.center_hz) / width) ** 2
        powers.append(max(noise, tone))
    return dict(id=str(uuid.uuid4()), measured_at=datetime.now(timezone.utc).isoformat(),
                identity="SIMULATOR,RSA6265", demo=True, settings=settings,
                power_dbm=powers, acquisition="demo", warnings=["ДЕМОНСТРАЦИЯ: синтетические данные"])
