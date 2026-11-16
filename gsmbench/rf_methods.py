"""Port directions shared by planning, calculation, and connection guidance."""


ISOLATION_PORTS = {
    "lo_if": ("LO", "IF"),
    "lo_rf": ("LO", "RF"),
    "rf_if": ("RF", "IF"),
    "rf_lo": ("RF", "LO"),
    "if_rf": ("IF", "RF"),
    "if_lo": ("IF", "LO"),
}
METHOD_CHOICES = [("Спектр", "spectrum"), ("Потери RF → IF", "conversion")] + [
    (f"Развязка {source} → {destination}", key) for key, (source, destination) in ISOLATION_PORTS.items()]


def injection_port(method):
    return ISOLATION_PORTS.get(method, ("RF", "IF"))[0]


def target_frequency(context):
    method = context.get("method", "spectrum")
    port = injection_port(method) if method in ISOLATION_PORTS else "IF"
    return context[port.lower() + "_hz"]


def injection_loss(context, settings):
    return settings.get(injection_port(context.get("method")).lower() + "_loss_db", 0)
