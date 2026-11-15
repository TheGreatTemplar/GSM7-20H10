"""Render the SVG to PNG and a Windows ICO containing multiple PNG resolutions."""

from io import BytesIO
from pathlib import Path
import struct

from PySide6 import QtCore, QtGui, QtSvg


def build():
    app = QtGui.QGuiApplication.instance() or QtGui.QGuiApplication([])
    assets = Path(__file__).resolve().parent.parent / "assets"
    renderer = QtSvg.QSvgRenderer(str(assets / "gsmbench.svg"))
    if not renderer.isValid():
        raise ValueError("Invalid SVG")
    entries = []
    for size in (16, 24, 32, 48, 64, 128, 256, 512):
        bitmap = QtGui.QImage(size, size, QtGui.QImage.Format.Format_ARGB32)
        bitmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(bitmap)
        renderer.render(painter)
        painter.end()
        if size == 512:
            if not bitmap.save(str(assets / "gsmbench.png")):
                raise OSError("Cannot save PNG")
            continue
        buffer = QtCore.QBuffer()
        buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        if not bitmap.save(buffer, "PNG"):
            raise OSError("Cannot encode icon")
        entries.append((size, bytes(buffer.data())))
    output = BytesIO()
    output.write(struct.pack("<HHH", 0, 1, len(entries)))
    offset = 6 + 16 * len(entries)
    for size, data in entries:
        dimension = 0 if size == 256 else size
        output.write(struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(data), offset))
        offset += len(data)
    for _, data in entries:
        output.write(data)
    (assets / "gsmbench.ico").write_bytes(output.getvalue())
    return app


if __name__ == "__main__":
    build()
