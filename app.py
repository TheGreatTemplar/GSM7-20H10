"""Desktop entry point."""

import sys

from PySide6 import QtGui, QtWidgets

from gsmbench import __version__
from gsmbench.ui import ICON_PATH, Window


def main():
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("TheGreatTemplar.GSM7-20H10.Testbench")
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("GSM7-20H10 Testbench")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("TheGreatTemplar")
    app.setWindowIcon(QtGui.QIcon(str(ICON_PATH)))
    app.setStyle("Fusion")
    window = Window()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
