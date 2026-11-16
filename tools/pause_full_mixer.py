"""Request a live full-test stop after the last P1dB curve of a given band."""

import argparse
import json
from pathlib import Path
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("--after-band", type=int, required=True)
    args = parser.parse_args()
    folder = args.folder.resolve()
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    band = next(item for item in manifest["bands"] if item[0] == args.after_band)
    deadline = time.monotonic() + 7200
    while time.monotonic() < deadline:
        try:
            checkpoint = json.loads((folder / "checkpoint.json").read_text(encoding="utf-8"))
            curves = json.loads((folder / "p1db.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Windows may briefly deny access while the acquisition replaces a file.
            time.sleep(0.05)
            continue
        if checkpoint["state"] != "running":
            print("Run already ended:", checkpoint["state"], flush=True)
            return
        finished = [e for e in curves if e["band_m"] == band[0]]
        if len(finished) == 33:
            (folder / "stop.request").write_text(
                f"User requested pause after {band[0]} m including all 33 P1dB curves.\n", encoding="utf-8")
            print("PAUSE REQUESTED AFTER BAND", band[0], "LAST RF", band[2], flush=True)
            return
        time.sleep(0.05)
    raise TimeoutError("Pause boundary not reached in two hours")


if __name__ == "__main__":
    main()
