"""Host-only Qt fixture for combined-canvas and cross-screen input checks.

Creates a temporary full-screen window on each host screen. Close the process
after testing to restore the underlying desktop. JSON state is the independent
input oracle; screenshot coordinates must come from scu's returned images.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main():
    from PySide6 import QtCore, QtGui, QtWidgets

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", type=Path)
    parser.add_argument("--token", default="cobalt 7319")
    args = parser.parse_args()
    app = QtWidgets.QApplication([])
    state = {"token": args.token, "screens": {}, "drags": []}

    def save():
        temporary = args.state.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False) + "\n")
        temporary.replace(args.state)

    class Surface(QtWidgets.QLabel):
        def __init__(self, screen_name, screen_state):
            super().__init__("Press here and drag to the other screen")
            self.screen_name = screen_name
            self.screen_state = screen_state
            self.start = None
            self.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.setFixedSize(560, 180)
            self.setStyleSheet("background:#833d54;color:white;border:3px solid white")

        def mousePressEvent(self, event):
            self.start = event.globalPosition()

        def mouseReleaseEvent(self, event):
            end = event.globalPosition()
            if self.start is not None and (end - self.start).manhattanLength() > 10:
                screen = app.screenAt(end.toPoint())
                state["drags"].append({
                    "from_screen": self.screen_name,
                    "to_screen": screen.name() if screen else None,
                    "from": [self.start.x(), self.start.y()],
                    "to": [end.x(), end.y()],
                })
                save()
            self.start = None

        def wheelEvent(self, event):
            self.screen_state["scrolls"] += 1
            save()
            event.accept()

    windows = []
    for screen in app.screens():
        screen_name = screen.name()
        screen_state = {"clicks": 0, "text": "", "scrolls": 0, "shortcuts": 0}
        state["screens"][screen_name] = screen_state
        window = QtWidgets.QWidget()
        window.setWindowTitle("Spaces canvas fixture " + screen_name)
        window.setStyleSheet("font-size:18px;background:#203442;color:#f5f5f5")
        layout = QtWidgets.QVBoxLayout(window)
        layout.addStretch()
        heading = QtWidgets.QLabel("SCU canvas fixture: " + screen_name)
        heading.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(heading)
        label = QtWidgets.QLabel("Native-detail check: " + args.token)
        label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
        button = QtWidgets.QPushButton("Increment this screen's counter")
        button.setFixedSize(560, 48)

        def clicked(_checked=False, item=screen_state):
            item["clicks"] += 1
            save()

        button.clicked.connect(clicked)
        layout.addWidget(button, alignment=QtCore.Qt.AlignmentFlag.AlignHCenter)
        entry = QtWidgets.QLineEdit()
        entry.setFixedSize(560, 48)
        entry.setPlaceholderText("Type Unicode here")

        def text_changed(text, item=screen_state):
            item["text"] = text
            save()

        entry.textChanged.connect(text_changed)
        layout.addWidget(entry, alignment=QtCore.Qt.AlignmentFlag.AlignHCenter)
        surface = Surface(screen_name, screen_state)
        layout.addWidget(surface, alignment=QtCore.Qt.AlignmentFlag.AlignHCenter)
        shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+K"), window)

        def shortcut_used(item=screen_state):
            item["shortcuts"] += 1
            save()

        shortcut.activated.connect(shortcut_used)
        layout.addStretch()
        window.winId()
        window.windowHandle().setScreen(screen)
        window.showFullScreen()
        windows.append(window)
    save()
    print("ready", flush=True)
    app.exec()


if __name__ == "__main__":
    main()
