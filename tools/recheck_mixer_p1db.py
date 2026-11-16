"""Bounded P1dB recheck; RF outputs require --run and an explicit upper limit."""

import argparse
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import threading
import time
import uuid

from gsmbench.rf_storage import SpectrumStore
from gsmbench.rsa import AnalyzerConfig, Rsa6265, frequencies, mixer_result, peak_near
from gsmbench.smu import Smu200a, SourcePlan, verified_off
from tools.full_mixer_test import atomic_json, p1db_search


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--parent", required=True, type=Path)
    parser.add_argument("--max-rf-dbm", required=True, type=int, choices=(10, 20))
    args = parser.parse_args()
    if not args.run:
        parser.error("Нужен --run; сценарий включает RF для 51 МГц при LO +20 dBm")
    parent = args.parent.resolve()
    manifest = json.loads((parent / "manifest.json").read_text(encoding="utf-8"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder = parent / "p1db-rechecks" / stamp
    folder.mkdir(parents=True)
    run_id = str(uuid.uuid4())
    store = SpectrumStore(manifest["database"], False)
    store.initialize()
    config = AnalyzerConfig(center_hz=11059200, span_hz=100000, rbw_hz=1000,
                           vbw_hz=1000, attenuation_db=30, reference_dbm=20, points=801)
    info = dict(run_id=run_id, parent_full_test_id=manifest["run_id"], rf_hz=51000000,
        lo_hz=62059200, if_hz=11059200, lo_dbm=20, rf_limit_dbm=args.max_rf_dbm,
        reference_rf_dbm=[-40, -30, -20], reference_repeats=5, search_repeats=3,
        analyzer_config=asdict(config), started_at=datetime.now(timezone.utc).isoformat(),
        database=manifest["database"], folder=str(folder))
    atomic_json(folder / "manifest.json", info)
    rows, means, errors = [], {}, []
    smu, rsa, estimate, off = None, None, None, None
    stop = threading.Event()
    enabled = False
    log = (folder / "scpi.log").open("w", encoding="utf-8", buffering=1)

    def record(line):
        log.write(datetime.now(timezone.utc).isoformat() + " " + line + "\n")

    def measure(power, repeat):
        nonlocal enabled
        if not -40 <= power <= args.max_rf_dbm:
            raise ValueError("Задание RF вне разрешённого диапазона")
        plan = SourcePlan(51000000, 62059200, power, 20, 2, 1)
        state = smu.setpoint(plan)
        if not enabled:
            state = smu.enable((1, 2), stop)
            enabled = True
        time.sleep(0.2)
        spectrum = rsa.acquire(stop)
        if spectrum["settings"] != actual_config or smu.state() != state:
            raise ValueError("Настройки приборов изменились во время измерения")
        marker = peak_near(spectrum, 11059200, 3000)
        background = statistics.median(value for hz, value in zip(frequencies(spectrum), spectrum["power_dbm"])
                                       if abs(hz - 11059200) > 10000)
        row = dict(spectrum_id=spectrum["id"], full_test_id=run_id, band_m=6, position="center",
            phase="p1db_recheck", rf_hz=51000000, lo_hz=62059200, if_hz=11059200,
            rf_dbm=state["channels"]["2"]["level_dbm"], lo_dbm=state["channels"]["1"]["level_dbm"],
            peak_hz=marker["frequency_hz"], output_dbm=marker["power_dbm"],
            observed_loss_db=power - marker["power_dbm"], background_dbm=background,
            peak_above_background_db=marker["power_dbm"] - background, status="uncalibrated",
            if_overload=spectrum.get("if_overload", False), measured_at=spectrum["measured_at"])
        spectrum["context"] = dict(sample=manifest["sample"], method="conversion", sources="SMU200A",
            notes="Повтор P1dB 51 МГц / LO +20 dBm; отдельная опора −40/−30/−20 dBm; без калибровки кабелей",
            rf_hz=51000000, lo_hz=62059200, if_hz=11059200, rf_channel=2, lo_channel=1,
            rf_set_dbm=power, lo_set_dbm=20, rf_actual_dbm=power, lo_actual_dbm=20,
            pin_dbm=power, pin_known=False, path_known=False, rf_loss_db=0, lo_loss_db=0,
            output_loss_db=0, marker_window_hz=3000, full_test_id=run_id,
            parent_full_test_id=manifest["run_id"], series_id=run_id, series_index=len(rows),
            band_m=6, position="center", phase="p1db_recheck", sweep_kind="rf_power", repeat=repeat)
        spectrum["generator"], spectrum["diagnostics"] = state, row
        spectrum["result"] = mixer_result(spectrum)
        atomic_json(folder / (spectrum["id"] + ".json"), spectrum)
        store.save_many([spectrum])
        rows.append(row)
        if row["if_overload"] or marker["power_dbm"] >= actual_config["reference_dbm"] - 0.5:
            raise ValueError("Перегрузка или пик около опорного уровня RSA")
        if row["peak_above_background_db"] < 20:
            raise ValueError("Превышение над фоном менее 20 дБ")
        return row

    def average(power):
        if power in means:
            return means[power]
        samples = [measure(power, index) for index in range(5 if power <= -20 else 3)]
        result = dict(samples[0], output_dbm=statistics.mean(row["output_dbm"] for row in samples),
            peak_above_background_db=min(row["peak_above_background_db"] for row in samples),
            sample_ids=[row["spectrum_id"] for row in samples],
            output_std_db=statistics.stdev(row["output_dbm"] for row in samples))
        result["observed_loss_db"] = power - result["output_dbm"]
        means[power] = result
        print(f"RF={power:g} dBm mean IF={result['output_dbm']:.4f} dBm std={result['output_std_db']:.4f} dB", flush=True)
        atomic_json(folder / "means.json", list(means.values()))
        return result

    state = "complete"
    try:
        smu = Smu200a(manifest["settings"]["smu_host"], manifest["settings"]["smu_port"], record)
        smu.all_off()
        rsa = Rsa6265(manifest["settings"]["host"], record)
        actual_config = rsa.configure(config)
        smu.prepare(SourcePlan(51000000, 62059200, -40, 20, 2, 1))
        smu.validate_plan(SourcePlan(51000000, 62059200, args.max_rf_dbm, 20, 2, 1))
        coarse = [average(power) for power in (-40, -30, -20, -10, 0)]
        estimate = p1db_search(coarse, average, rf_limit_dbm=args.max_rf_dbm)
        estimate.update(band_m=6, position="center", rf_hz=51000000, lo_dbm=20,
                        recheck_id=run_id, recheck_folder=str(folder))
    except Exception as error:
        state = "failed"
        errors.append(str(error))
        print("ERROR", error, flush=True)
    finally:
        if smu:
            try:
                off = dict(known=True, state=verified_off(smu))
            except Exception as error:
                off = dict(known=False, error=str(error))
                errors.append(str(error))
                state = "failed"
            smu.close()
        if rsa:
            rsa.close()
        result = dict(manifest=info, state=state, ended_at=datetime.now(timezone.utc).isoformat(),
                      estimate=estimate, rows=rows, means=list(means.values()), errors=errors, final_generator=off)
        atomic_json(folder / "report.json", result)
        log.close()
    print("REPORT", folder, flush=True)
    print("RESULT", json.dumps(estimate, ensure_ascii=True), flush=True)
    print("BOTH OFF", bool(off and off["known"]), flush=True)
    return int(state != "complete" or not estimate or estimate["status"] != "measured_uncalibrated")


if __name__ == "__main__":
    raise SystemExit(main())
