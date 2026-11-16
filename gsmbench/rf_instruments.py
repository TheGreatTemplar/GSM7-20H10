"""Read-only LAN commissioning for RSA6265 and SMU200A.

This initial commissioning layer intentionally exposes no RF enable operation.
Measurement-specific drivers will use the verified firmware/options inventory.
"""

import ipaddress


def lan_resource(host):
    host = str(ipaddress.IPv4Address(host.strip()))
    return f"TCPIP0::{host}::inst0::INSTR"


def socket_resource(host, port):
    host = str(ipaddress.IPv4Address(host.strip()))
    if not 1 <= int(port) <= 65535:
        raise ValueError("TCP-порт должен быть в пределах 1…65535")
    return f"TCPIP0::{host}::{int(port)}::SOCKET"


def identify_resource(resource, expected_model, options=False, manager_factory=None):
    """Open VXI-11, query identity/options, close even on errors; never reset."""
    if not resource.upper().startswith("TCPIP") or not resource.upper().endswith(("::INSTR", "::SOCKET")):
        raise ValueError("Для проверки LAN нужен ресурс TCPIP…::INSTR или TCPIP…::SOCKET")
    if manager_factory is None:
        import pyvisa
        manager_factory = pyvisa.ResourceManager
    manager = manager_factory("@py")
    device = None
    try:
        device = manager.open_resource(resource, open_timeout=3000)
        device.timeout = 3000
        device.write_termination = "\n"
        device.read_termination = "\n"
        identity = device.query("*IDN?").strip()
        fields = identity.split(",")
        if len(fields) < 2 or fields[1].strip().upper() != expected_model.upper():
            raise ValueError(f"Ожидался {expected_model}, получен: {identity}")
        result = dict(resource=resource, identity=identity)
        if options:
            result["options"] = device.query("*OPT?").strip()
        return result
    finally:
        try:
            if device is not None:
                device.close()
        finally:
            manager.close()
