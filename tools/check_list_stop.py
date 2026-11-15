"""Hardware diagnostic: 4 (or --long: 20) points at 1–4 uA, 1 V compliance."""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gsmbench.core import Recipe, generate_levels
from gsmbench.instrument import Instrument, SocketTransport, VisaTransport


def main():
    started = time.monotonic()
    stop = threading.Event()
    timer = None

    def log(message):
        nonlocal timer
        print(f"{time.monotonic() - started:.3f} {message}", flush=True)
        if message == "> :INIT" and "--no-stop" not in sys.argv:
            timer = threading.Timer(10 if "--long" in sys.argv else 1.2, stop.set)
            timer.start()

    transport = (VisaTransport("TCPIP0::172.16.236.32::5025::SOCKET", log) if "--visa" in sys.argv
                 else SocketTransport("172.16.236.32", 5025, log))
    instrument = Instrument(transport)
    try:
        instrument.identify()
        points = []
        levels = generate_levels(1e-6, 4e-6, 20) if "--long" in sys.argv else (1e-6, 2e-6, 3e-6, 4e-6)
        instrument.measure(Recipe(levels=levels, delay=1), stop, points.append)
        log(f"Cancelled: {stop.is_set()}, returned points: {len(points)}")
        log("OUTPUT OFF confirmed")
        log("Stored points: " + transport.query(":TRAC:POIN:ACT?"))
    finally:
        if timer is not None:
            timer.cancel()
        try:
            instrument.output_off()
        finally:
            transport.close()


if __name__ == "__main__":
    main()
