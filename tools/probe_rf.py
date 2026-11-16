r"""Read identity/options over LAN without changing instrument state or outputs.

Run from the project root: .venv\Scripts\python.exe -m tools.probe_rf --help
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from gsmbench.rf_instruments import identify_resource, lan_resource, socket_resource


def main():
    parser = argparse.ArgumentParser(description="Проверка Ethernet RSA6265 / SMU200A без включения выходов")
    parser.add_argument("--rsa", help="IPv4 RSA6265")
    parser.add_argument("--smu", help="IPv4 SMU200A")
    parser.add_argument("--smu-port", type=int, help="SCPI TCP-порт SMU, например 5025; без него используется VXI-11")
    args = parser.parse_args()
    if not args.rsa and not args.smu:
        parser.error("Укажите --rsa IP и/или --smu IP")
    inventory = dict(measured_at=datetime.now(timezone.utc).isoformat(), instruments={})
    failed = False
    for host, model, options in ((args.rsa, "RSA6265", False), (args.smu, "SMU200A", True)):
        if host:
            try:
                resource = socket_resource(host, args.smu_port) if model == "SMU200A" and args.smu_port else lan_resource(host)
                result = identify_resource(resource, model, options)
                inventory["instruments"][model] = result
                print(model + ": " + json.dumps(result, ensure_ascii=False))
            except Exception as error:
                inventory["instruments"][model] = dict(error=str(error))
                print(model + ": ОШИБКА: " + str(error))
                failed = True
    target = Path(__file__).resolve().parent.parent / "data" / "rf-inventory"
    target.mkdir(parents=True, exist_ok=True)
    path = target / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    path.write_text(json.dumps(inventory, ensure_ascii=False, indent=4), encoding="utf-8")
    print("Протокол: " + str(path))
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
