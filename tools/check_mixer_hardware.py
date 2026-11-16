"""Bounded live mixer checks using the application acquisition engine and archive.

RF output is enabled only with --run. The current GUI settings supply addresses,
IF and mixer ID. This tool does not overwrite those settings or assume calibration.
"""

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import sys

from PySide6 import QtCore

from gsmbench.rf_storage import SpectrumStore
from gsmbench.rsa import AnalyzerConfig, frequencies, peak_near
from gsmbench.rsa_ui import RsaWorker


ROOT = Path(__file__).resolve().parent.parent


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description="Реальный контрольный прогон смесителя; A/1=LO, B/2=RF")
    parser.add_argument("--run", action="store_true", help="Разрешить включение RF во время теста")
    parser.add_argument("--matrix", action="store_true", help="КВ: 10 частот x RF -40/-30/-20 x LO 7/10/13 dBm")
    parser.add_argument("--lo-dbm", type=float, default=10)
    args = parser.parse_args()
    if not args.run:
        parser.error("Для аппаратного прогона укажите --run")
    if not 0 <= args.lo_dbm <= 13:
        parser.error("Контрольный тест ограничен LO 0…13 dBm")
    app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
    saved = json.loads((ROOT / "settings.local.json").read_text(encoding="utf-8")).get("rsa", {})
    intermediate = saved.get("if_hz", 11059200)
    rf = saved.get("rf_mhz", 14) * 1e6
    side = 1 if saved.get("lo_side", "upper") == "upper" else -1
    context = dict(sample=saved.get("sample", "MX001"),
        notes="Реальная аппаратная проверка стенда; кабельные поправки не калиброваны", method="conversion",
        rf_hz=rf, lo_hz=rf + side * intermediate, if_hz=intermediate, sources="SMU200A",
        pin_dbm=-40, pin_known=False, path_known=False, output_loss_db=0, marker_window_hz=3000)
    options = dict(host=saved.get("smu_host", "169.254.234.52"), port=saved.get("smu_port", 5025),
        rf_channel=2, lo_channel=1, rf_dbm=-40, lo_dbm=args.lo_dbm,
        rf_loss_db=0, lo_loss_db=0, source_path_known=False, settling_s=0.3,
        sweep_kind="matrix" if args.matrix else "rf_power", sweep_start=-40, sweep_stop=-20,
        matrix_rf_mhz="1.85;3.65;5.35;7.1;10.12;14.15;18.1;21.2;24.93;28.5",
        matrix_rf_dbm="-40;-30;-20", matrix_lo_dbm="7;10;13")
    config = AnalyzerConfig(center_hz=intermediate, span_hz=100000, rbw_hz=1000,
                            vbw_hz=1000, points=801, attenuation_db=10, reference_dbm=0)
    store = SpectrumStore(saved.get("database", str(ROOT / "data" / "mixers.db")), False)
    store.initialize()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = ROOT / "data" / "rf-hardware-checks" / stamp
    target.mkdir(parents=True, exist_ok=True)
    worker = RsaWorker(saved.get("host", "172.16.236.90"), False, "series", config, context,
                       90 if args.matrix else 5, 0.1, None, options)
    rows, errors = [], []
    final_state = {}
    with (target / "scpi.log").open("w", encoding="utf-8", buffering=1) as log:
        worker.log.connect(lambda line: log.write(line + "\n"))
        worker.failed.connect(lambda error: (errors.append(error), print("ERROR:", error, flush=True)))

        def generator_state(event):
            final_state.clear()
            final_state.update(event)

        worker.generator_state.connect(generator_state)

        def receive(spectrum):
            recovery = ROOT / "data" / "rf-pending" / (spectrum["id"] + ".json")
            try:
                point = spectrum["context"]
                marker = peak_near(spectrum, intermediate, 3000)
                axis = frequencies(spectrum)
                background = [power for hz, power in zip(axis, spectrum["power_dbm"]) if abs(hz - intermediate) > 10000]
                noise = statistics.median(background)
                snr = marker["power_dbm"] - noise
                observed_loss = point["rf_actual_dbm"] - marker["power_dbm"]
                row = dict(spectrum_id=spectrum["id"], series_id=point["series_id"],
                    rf_hz=point["rf_hz"], lo_hz=point["lo_hz"], if_hz=intermediate,
                    rf_dbm=point["rf_actual_dbm"], lo_dbm=point["lo_actual_dbm"],
                    peak_hz=marker["frequency_hz"], output_dbm=marker["power_dbm"],
                    observed_loss_db=observed_loss, background_dbm=noise, peak_above_background_db=snr,
                    status="uncalibrated" if snr >= 15 else "low_snr")
                spectrum["diagnostics"] = dict(row,
                    note="Генератор → RSA, без кабельных поправок; перегрузка автоматически не проверена")
                recovery.parent.mkdir(parents=True, exist_ok=True)
                recovery.write_text(json.dumps(dict(database=str(store.path), spectrum=spectrum),
                                               ensure_ascii=False, indent=4), encoding="utf-8")
                store.save_many([spectrum])
                recovery.unlink()
                rows.append(row)
                print(f"{len(rows)}: RF={point['rf_hz'] / 1e6:.6f} MHz, "
                      f"RF={point['rf_actual_dbm']:.2f} dBm, LO={point['lo_actual_dbm']:.2f} dBm; "
                      f"IF={marker['frequency_hz'] / 1e6:.6f} MHz, Pout={marker['power_dbm']:.3f} dBm, "
                      f"raw loss={observed_loss:.3f} dB, peak/noise={snr:.1f} dB", flush=True)
                if snr < 15:
                    errors.append("Нет уверенного тона ПЧ: остановлено после точки с низким SNR")
                    worker.stop.set()
            except Exception as error:
                errors.append(str(error))
                worker.stop.set()
                print("SAVE/ANALYSIS ERROR:", error, flush=True)

        worker.spectrum.connect(receive)
        worker.run()
    if rows:
        with (target / "summary.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]), delimiter=";")
            writer.writeheader()
            writer.writerows(rows)
    off = final_state.get("known", False) and bool(final_state.get("state", {}).get("channels")) and all(
        channel["output"] == 0 for channel in final_state["state"]["channels"].values())
    report = dict(database=str(store.path), count=len(rows), errors=errors, outputs_off_confirmed=off,
                  final_generator=final_state, rows=rows)
    (target / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=4), encoding="utf-8")
    print("REPORT:", target, flush=True)
    print("DATABASE:", store.path, flush=True)
    print("BOTH RF OUTPUTS OFF:", off, flush=True)
    return int(bool(errors) or not off or not rows)


if __name__ == "__main__":
    raise SystemExit(main())
