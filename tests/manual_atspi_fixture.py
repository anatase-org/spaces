"""Host-only GTK/Qt fixture with independently observable action state."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("toolkit", choices=("gtk", "qt"))
    parser.add_argument("token")
    parser.add_argument("state", type=Path)
    args = parser.parse_args()
    state = {"clicks": 0, "text": "", "checked": False}

    def update(**values):
        state.update(values)
        temporary = args.state.with_suffix(".tmp")
        temporary.write_text(json.dumps(state) + "\n")
        temporary.replace(args.state)

    title = "Spaces AT-SPI fixture " + args.token
    if args.toolkit == "gtk":
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk

        window = Gtk.Window(title=title)
        window.connect("destroy", Gtk.main_quit)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        window.add(box)
        button = Gtk.Button(label="Spaces fixture button")
        entry = Gtk.Entry()
        entry.get_accessible().set_name("Spaces fixture entry")
        checkbox = Gtk.CheckButton(label="Spaces fixture checkbox")
        button.connect(
            "clicked", lambda *_: update(clicks=state["clicks"] + 1)
        )
        entry.connect("changed", lambda item: update(text=item.get_text()))
        checkbox.connect(
            "toggled", lambda item: update(checked=item.get_active())
        )
        for widget in (button, entry, checkbox):
            box.pack_start(widget, False, False, 0)
        window.show_all()
        update()
        Gtk.main()
    else:
        from PySide6 import QtWidgets

        app = QtWidgets.QApplication([])
        window = QtWidgets.QWidget()
        window.setWindowTitle(title)
        box = QtWidgets.QVBoxLayout(window)
        button = QtWidgets.QPushButton("Spaces fixture button")
        entry = QtWidgets.QLineEdit()
        entry.setAccessibleName("Spaces fixture entry")
        checkbox = QtWidgets.QCheckBox("Spaces fixture checkbox")
        button.clicked.connect(lambda: update(clicks=state["clicks"] + 1))
        entry.textChanged.connect(lambda text: update(text=text))
        checkbox.toggled.connect(lambda checked: update(checked=checked))
        for widget in (button, entry, checkbox):
            box.addWidget(widget)
        window.show()
        update()
        app.exec()


if __name__ == "__main__":
    main()
