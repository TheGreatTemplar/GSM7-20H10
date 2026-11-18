"""Prepare RF archive rows without touching widgets or instruments."""

from PySide6 import QtCore

from .rf_methods import METHOD_CHOICES
from .rsa import mixer_result


def archive_rows(spectra, cancelled=lambda: False):
    labels = {key: label for label, key in METHOD_CHOICES}
    labels.update(two_tone_pa="PA · два тона", two_tone_mixer="Смеситель · два тона", harmonic_pa="PA · гармоническая полоса")
    rows = []
    for spectrum in spectra:
        if cancelled():
            return None
        context = spectrum.get("context", {})
        result = mixer_result(spectrum)
        method = context.get("method", "spectrum")
        reason = result.get("reason", "")
        values = [context.get("sample", ""), context.get("batch", ""), context.get("model", ""),
            spectrum["measured_at"][:19], labels.get(method, method),
            f"{context.get('rf_hz', 0) / 1e6:.6f}", f"{context.get('lo_hz', 0) / 1e6:.6f}", f"{context.get('if_hz', 0) / 1e6:.6f}",
            f"{context['rf_actual_dbm']:.3f}" if "rf_actual_dbm" in context else "",
            f"{context['lo_actual_dbm']:.3f}" if "lo_actual_dbm" in context else "",
            f"{result['value_db']:.3f}" if result.get("value_db") is not None else "—",
            {"preliminary": "Предварительно", "not_calculated": "Нет расчёта", "spectrum_only": "Спектр"}[result["status"]],
            context.get("notes", ""), reason]
        rows.append((values, result["status"] == "not_calculated"))
    return rows


class ArchiveLoader(QtCore.QThread):
    ready = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, store, parent):
        super().__init__(parent)
        self.store = store

    def run(self):
        try:
            spectra = self.store.load()
            rows = archive_rows(spectra, self.isInterruptionRequested)
            if rows is not None and not self.isInterruptionRequested():
                self.ready.emit((spectra, rows))
        except Exception as error:
            self.failed.emit(str(error))
