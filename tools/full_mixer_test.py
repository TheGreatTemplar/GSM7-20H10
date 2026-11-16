"""Full HF edge/centre survey and adaptive P1dB, with an incremental archive.

Requires --run. RF above 0 dBm is confined to the explicitly requested P1dB
search, capped at +10 dBm. A stop.request file in the run folder requests OFF.
"""

import argparse
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
import csv
import json
from pathlib import Path
import sqlite3
import statistics
import sys
import threading
import time
import uuid

from gsmbench.rf_sequence import plan_sequence
from gsmbench.rf_storage import SpectrumStore
from gsmbench.rsa import AcquisitionCancelled, AnalyzerConfig, Rsa6265, frequencies, mixer_result, peak_near
from gsmbench.smu import Smu200a, SourcePlan, verified_off


ROOT = Path(__file__).resolve().parent.parent
BANDS = [(160, 1810000, 2000000), (80, 3500000, 3800000), (60, 5351500, 5366500),
         (40, 7000000, 7200000), (30, 10100000, 10150000), (20, 14000000, 14350000),
         (17, 18068000, 18168000), (15, 21000000, 21450000), (12, 24890000, 24990000),
         (10, 28000000, 29700000)]


def atomic_json(path, payload):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def p1db_search(coarse, acquire, rf_limit_dbm=10):
    """Input is referenced to the generator connector, not a calibrated DUT port."""
    if type(rf_limit_dbm) is not int or not 2 <= rf_limit_dbm <= 20 or rf_limit_dbm % 2:
        raise ValueError("Предел поиска RF должен быть чётным целым от +2 до +20 dBm")
    coarse = sorted(coarse, key=lambda row: row["rf_dbm"])
    reference = coarse[:3]
    if len(reference) < 3 or min(row["peak_above_background_db"] for row in reference) < 20:
        return dict(status="invalid_reference", reason="Недостаточный SNR опорных точек")
    gains = [row["output_dbm"] - row["rf_dbm"] for row in reference]
    spread = max(gains) - min(gains)
    if spread > 0.3:
        return dict(status="invalid_reference", reason="Разброс опорного усиления более 0,3 дБ", reference_spread_db=spread)
    gain = statistics.mean(gains)

    def compression(row):
        return gain - (row["output_dbm"] - row["rf_dbm"])

    low, high = None, None
    for previous, current in zip(coarse, coarse[1:]):
        if compression(previous) < 1 <= compression(current):
            low, high = previous, current
            break
    if high is None:
        low = coarse[-1]
        for power in range(2, rf_limit_dbm + 1, 2):
            current = acquire(power)
            if compression(current) >= 1:
                high = current
                break
            low = current
    if high is None:
        return dict(status="not_reached", input_lower_bound_dbm=rf_limit_dbm, small_signal_gain_db=gain,
                    reference_spread_db=spread, compression_at_limit_db=compression(low))
    while high["rf_dbm"] - low["rf_dbm"] > 0.5:
        current = acquire(round((high["rf_dbm"] + low["rf_dbm"]) / 2, 2))
        if compression(current) >= 1:
            high = current
        else:
            low = current
    clow, chigh = compression(low), compression(high)
    power = low["rf_dbm"] + (1 - clow) / (chigh - clow) * (high["rf_dbm"] - low["rf_dbm"])
    return dict(status="measured_uncalibrated", input_generator_p1db_dbm=power,
                output_rsa_p1db_dbm=power + gain - 1, bracket_low_dbm=low["rf_dbm"],
                bracket_high_dbm=high["rf_dbm"], small_signal_gain_db=gain,
                reference_spread_db=spread, reference_points=[row["spectrum_id"] for row in reference])


class FullTest:
    def __init__(self, folder, manifest, pause_after_band=None):
        self.folder, self.manifest = folder, manifest
        self.pause_after_band = pause_after_band
        self.planned_matrix_points = len(manifest["bands"]) * 3 * len(manifest["matrix_rf_dbm"]) * len(manifest["lo_dbm"])
        self.store = SpectrumStore(manifest["database"], False)
        self.store.initialize()
        self.rows = []
        self.cache = {}
        self.estimates = []
        self.completed_bands = []
        self.errors = []
        self.smu = None
        self.rsa = None
        self.last_state = None
        self.stop = threading.Event()
        self.done = threading.Event()
        self.started = time.monotonic()
        self.sequence_index = 0
        with closing(sqlite3.connect(self.store.path.as_uri() + "?mode=ro", uri=True)) as db:
            for payload, in db.execute("SELECT payload FROM rf_spectrum WHERE json_extract(payload, '$.context.full_test_id')=?",
                                      (manifest["run_id"],)):
                spectrum = json.loads(payload)
                row = spectrum["diagnostics"]
                self.rows.append(row)
                self.cache[self.key(row["band_m"], row["rf_hz"], row["rf_dbm"], row["lo_dbm"], row["phase"])] = row
        if (folder / "p1db.json").exists():
            self.estimates = json.loads((folder / "p1db.json").read_text(encoding="utf-8"))
        if (folder / "checkpoint.json").exists():
            self.completed_bands = json.loads((folder / "checkpoint.json").read_text(encoding="utf-8")).get("completed_bands", [])
        self.log_file = (folder / "scpi.log").open("a", encoding="utf-8", buffering=1)
        self.journal = (folder / "journal.jsonl").open("a", encoding="utf-8", buffering=1)
        self.csv_file = (folder / "summary.csv").open("a", encoding="utf-8-sig", newline="", buffering=1)
        self.csv_writer = None
        self.watcher = threading.Thread(target=self.watch_stop, daemon=True)

    def watch_stop(self):
        while not self.done.wait(0.2):
            if (self.folder / "stop.request").exists():
                self.stop.set()

    @staticmethod
    def key(band, rf, power, lo, phase):
        return (int(band), round(rf), round(power, 2), round(lo, 2), phase)

    def log(self, line):
        self.log_file.write(datetime.now(timezone.utc).isoformat() + " " + line + "\n")

    def checkpoint(self, state="running"):
        atomic_json(self.folder / "checkpoint.json", dict(run_id=self.manifest["run_id"], state=state,
            updated_at=datetime.now(timezone.utc).isoformat(), total_spectra=len(self.rows),
            matrix_points=sum(row["phase"] == "matrix" for row in self.rows),
            planned_matrix_points=self.planned_matrix_points, p1db_curves=len(self.estimates), completed_bands=self.completed_bands,
            last_point=self.rows[-1] if self.rows else None, last_generator=self.last_state, errors=self.errors))

    def measure(self, band, position, rf, power, lo_level, phase):
        key = self.key(band, rf, power, lo_level, phase)
        if key in self.cache:
            return self.cache[key]
        if self.stop.is_set():
            raise AcquisitionCancelled()
        settings = self.manifest["settings"]
        intermediate = self.manifest["if_hz"]
        plan = SourcePlan(rf, rf + intermediate, power, lo_level, 2, 1)
        generator = self.smu.setpoint(plan)
        if not self.outputs_enabled:
            generator = self.smu.enable((1, 2), self.stop)
            self.outputs_enabled = True
        if self.stop.wait(0.1):
            raise AcquisitionCancelled()
        spectrum = self.rsa.acquire(self.stop)
        if spectrum["settings"] != self.rsa_settings or self.smu.state() != generator:
            raise RuntimeError("Параметры приборов изменились во время измерения")
        marker = peak_near(spectrum, intermediate, 3000)
        background = statistics.median(power for hz, power in zip(frequencies(spectrum), spectrum["power_dbm"])
                                       if abs(hz - intermediate) > 10000)
        actual_rf = generator["channels"]["2"]["level_dbm"]
        actual_lo = generator["channels"]["1"]["level_dbm"]
        snr = marker["power_dbm"] - background
        row = dict(spectrum_id=spectrum["id"], full_test_id=self.manifest["run_id"], band_m=band,
            position=position, phase=phase, rf_hz=round(rf), lo_hz=round(rf + intermediate), if_hz=intermediate,
            rf_dbm=actual_rf, lo_dbm=actual_lo, peak_hz=marker["frequency_hz"], output_dbm=marker["power_dbm"],
            observed_loss_db=actual_rf - marker["power_dbm"], background_dbm=background,
            peak_above_background_db=snr, status="uncalibrated" if snr >= 15 else "low_snr",
            if_overload=spectrum.get("if_overload", False),
            measured_at=spectrum["measured_at"])
        spectrum["context"] = dict(sample=self.manifest["sample"], method="conversion", sources="SMU200A",
            notes=f"Полный КВ тест {self.manifest['run_id']} {band} м {position} {phase}; без калибровки кабелей",
            rf_hz=rf, lo_hz=rf + intermediate, if_hz=intermediate,
            rf_channel=2, lo_channel=1, rf_set_dbm=power, lo_set_dbm=lo_level,
            rf_actual_dbm=actual_rf, lo_actual_dbm=actual_lo, pin_dbm=actual_rf, pin_known=False,
            rf_loss_db=0, lo_loss_db=0, output_loss_db=0, path_known=False, marker_window_hz=3000,
            full_test_id=self.manifest["run_id"], band_m=band, position=position, phase=phase,
            series_id=self.series_id, series_index=self.sequence_index, sweep_kind="matrix")
        spectrum["generator"] = generator
        spectrum["diagnostics"] = row
        spectrum["result"] = mixer_result(spectrum)
        recovery = ROOT / "data" / "rf-pending" / (spectrum["id"] + ".json")
        recovery.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(recovery, dict(database=str(self.store.path), spectrum=spectrum))
        self.store.save_many([spectrum])
        recovery.unlink()
        if self.csv_writer is None:
            self.csv_writer = csv.DictWriter(self.csv_file, fieldnames=list(row), delimiter=";")
            if self.csv_file.tell() == 0:
                self.csv_writer.writeheader()
        self.csv_writer.writerow(row)
        self.csv_file.flush()
        self.journal.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.rows.append(row)
        self.cache[key] = row
        self.sequence_index += 1
        if len(self.rows) % 10 == 0:
            self.checkpoint()
            base = sum(item["phase"] == "matrix" for item in self.rows)
            print(f"BASE {base}/{self.planned_matrix_points} TOTAL {len(self.rows)} BAND {band}m {position} RF={rf/1e6:.6f} MHz "
                  f"RF={actual_rf:g} LO={actual_lo:g} Pout={marker['power_dbm']:.2f} loss={row['observed_loss_db']:.2f}", flush=True)
        if spectrum.get("if_overload") or marker["power_dbm"] >= self.rsa_settings["reference_dbm"] - 0.5:
            raise RuntimeError("Пик достигает опорного уровня RSA; дальнейший прогон остановлен")
        return row

    def run_band(self, band, low, high):
        nodes = [("low", low), ("center", (low + high) // 2), ("high", high)]
        self.series_id = str(uuid.uuid4())
        self.outputs_enabled = False
        self.sequence_index = 0
        settings = self.manifest["settings"]
        self.smu = Smu200a(settings["smu_host"], settings["smu_port"], self.log)
        try:
            self.smu.all_off()
            self.rsa = Rsa6265(settings["host"], self.log)
            self.rsa_settings = self.rsa.configure(self.config)
            options = dict(host=settings["smu_host"], port=settings["smu_port"],
                rf_channel=2, lo_channel=1, rf_dbm=-60, lo_dbm=10, rf_loss_db=0, lo_loss_db=0,
                source_path_known=False, settling_s=0.1, sweep_kind="matrix",
                matrix_rf_mhz=";".join(f"{hz/1e6:.6f}" for _, hz in nodes),
                matrix_rf_dbm="-60;-50;-40;-30;-20;-10;0", matrix_lo_dbm=";".join(str(level) for level in range(10, 21)))
            context = dict(rf_hz=low, lo_hz=low + self.manifest["if_hz"], if_hz=self.manifest["if_hz"],
                           method="conversion", marker_window_hz=3000)
            plans = plan_sequence(self.config, context, options, 231)
            self.smu.prepare(plans[0][2])
            for _, _, plan in plans:
                self.smu.validate_plan(plan)
            for _, hz in nodes:
                for level in range(10, 21):
                    self.smu.validate_plan(SourcePlan(hz, hz + self.manifest["if_hz"], 10, level))
            print(f"BEGIN BAND {band}m: {low/1e6:.6f} / {(low+high)/2e6:.6f} / {high/1e6:.6f} MHz", flush=True)
            for _, point, plan in plans:
                position = min(nodes, key=lambda item: abs(item[1] - point["rf_hz"]))[0]
                self.measure(band, position, point["rf_hz"], plan.rf_dbm, plan.lo_dbm, "matrix")
            for position, hz in nodes:
                for lo_level in range(10, 21):
                    existing = next((item for item in self.estimates if item["band_m"] == band and item["rf_hz"] == hz and item["lo_dbm"] == lo_level), None)
                    if existing:
                        continue
                    coarse = [self.cache[self.key(band, hz, power, lo_level, "matrix")] for power in range(-60, 1, 10)]
                    estimate = p1db_search(coarse, lambda power: self.measure(band, position, hz, power, lo_level, "p1db"))
                    estimate.update(band_m=band, position=position, rf_hz=hz, lo_dbm=lo_level)
                    self.estimates.append(estimate)
                    atomic_json(self.folder / "p1db.json", self.estimates)
                    self.checkpoint()
            self.completed_bands.append(band)
        finally:
            try:
                self.last_state = dict(known=True, state=verified_off(self.smu))
                print(f"BAND {band}m BOTH OUTPUTS OFF CONFIRMED", flush=True)
            except Exception as error:
                self.last_state = dict(known=False, error=str(error))
                self.errors.append(str(error))
                raise
            finally:
                self.smu.close()
                self.smu = None
                if self.rsa:
                    self.rsa.close()
                    self.rsa = None
                self.checkpoint()

    def run(self):
        self.config = AnalyzerConfig(**self.manifest["analyzer_config"])
        self.watcher.start()
        state = "complete"
        try:
            for band, low, high in self.manifest["bands"]:
                if band not in self.completed_bands:
                    self.run_band(band, low, high)
                    if band == self.pause_after_band:
                        state = "paused"
                        break
        except AcquisitionCancelled:
            state = "stopped"
        except Exception as error:
            state = "failed"
            self.errors.append(str(error))
            print("ERROR:", error, flush=True)
        finally:
            self.done.set()
            self.watcher.join(timeout=1)
            self.checkpoint(state)
            atomic_json(self.folder / "report.json", dict(manifest=self.manifest, state=state,
                ended_at=datetime.now(timezone.utc).isoformat(), rows=self.rows, p1db=self.estimates,
                errors=self.errors, final_generator=self.last_state, completed_bands=self.completed_bands))
            if self.estimates:
                keys = ["band_m", "position", "rf_hz", "lo_dbm", "status", "input_generator_p1db_dbm",
                        "output_rsa_p1db_dbm", "bracket_low_dbm", "bracket_high_dbm", "input_lower_bound_dbm", "small_signal_gain_db"]
                with (self.folder / "p1db.csv").open("w", encoding="utf-8-sig", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=keys, delimiter=";", extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(self.estimates)
            self.log_file.close()
            self.journal.close()
            self.csv_file.close()
        print("FINAL", state, "spectra", len(self.rows), "P1dB curves", len(self.estimates), "REPORT", self.folder, flush=True)
        return int(state not in ("complete", "paused") or bool(self.errors))


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description="Полная КВ матрица 2310 точек и уточнение P1dB до +10 dBm")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--continue-paused", action="store_true", help="Archive the previous stop request when resuming")
    parser.add_argument("--pause-after-band", type=int, choices=[item[0] for item in BANDS],
                        help="Finish this band, verify both outputs OFF, and pause before the next one")
    args = parser.parse_args()
    if not args.run:
        parser.error("Нужен --run для управления реальными RF-выходами")
    if args.continue_paused and not args.resume:
        parser.error("--continue-paused используется вместе с --resume")
    if args.resume:
        folder = args.resume.resolve()
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        if args.pause_after_band and (folder / "checkpoint.json").exists():
            previous = json.loads((folder / "checkpoint.json").read_text(encoding="utf-8"))
            if args.pause_after_band in previous.get("completed_bands", []):
                parser.error("Диапазон для паузы уже завершён; выберите следующий незавершённый")
        stop_request = folder / "stop.request"
        if stop_request.exists():
            if not args.continue_paused:
                parser.error("Сохранён запрос остановки. Для продолжения добавьте --continue-paused")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            stop_request.rename(folder / ("stop.request." + stamp + ".handled"))
    else:
        saved = json.loads((ROOT / "settings.local.json").read_text(encoding="utf-8"))["rsa"]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        folder = ROOT / "data" / "rf-full-tests" / stamp
        folder.mkdir(parents=True, exist_ok=True)
        manifest = dict(run_id=str(uuid.uuid4()), started_at=datetime.now(timezone.utc).isoformat(),
            sample=saved.get("sample", "MX001"), database=saved.get("database", str(ROOT / "data" / "mixers.db")),
            settings={key: saved[key] for key in ("host", "smu_host", "smu_port")}, if_hz=11059200,
            bands=BANDS, analyzer_config=asdict(AnalyzerConfig(center_hz=11059200, span_hz=100000,
                rbw_hz=1000, vbw_hz=1000, points=801, attenuation_db=20, reference_dbm=10)),
            matrix_rf_dbm=list(range(-60, 1, 10)), lo_dbm=list(range(10, 21)), p1db_rf_limit_dbm=10,
            p1db_bracket_width_db=0.5, settling_s=0.1,
            reference="https://www.iaru-r1.org/wp-content/uploads/2019/08/hf_r1_bandplan.pdf")
        atomic_json(folder / "manifest.json", manifest)
    print("RUN FOLDER", folder, flush=True)
    print("STOP FILE", folder / "stop.request", flush=True)
    return FullTest(folder, manifest, args.pause_after_band).run()


if __name__ == "__main__":
    raise SystemExit(main())
