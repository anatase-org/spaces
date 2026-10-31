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
    parser.add_argument("--computer-use", action="store_true",
                        help="Add an independently measured scroll/drag surface")
    args = parser.parse_args()
    state = {"clicks": 0, "text": "", "checked": False}

    def update(**values):
        state.update(values)
        temporary = args.state.with_suffix(".tmp")
        temporary.write_text(json.dumps(state) + "\n")
        temporary.replace(args.state)

    if args.computer_use:
        state.update(scrolls=0, drags=0)
    title = "Spaces AT-SPI fixture " + args.token
    if args.toolkit == "gtk":
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk, Gtk

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
        if args.computer_use:
            surface = Gtk.EventBox()
            surface.set_size_request(300, 120)
            surface.add(Gtk.Label(label="Scroll here; drag here"))
            surface.add_events(Gdk.EventMask.SCROLL_MASK | Gdk.EventMask.BUTTON_PRESS_MASK |
                               Gdk.EventMask.BUTTON_RELEASE_MASK | Gdk.EventMask.POINTER_MOTION_MASK)
            start = [None]
            def press(_widget, event):
                start[0] = (event.x, event.y)
            def release(_widget, event):
                if start[0] is not None and abs(event.x - start[0][0]) + abs(event.y - start[0][1]) > 10:
                    update(drags=state["drags"] + 1)
                start[0] = None
            surface.connect("button-press-event", press)
            surface.connect("button-release-event", release)
            surface.connect("scroll-event", lambda *_: update(scrolls=state["scrolls"] + 1))
            box.pack_start(surface, True, True, 0)
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
        if args.computer_use:
            class Surface(QtWidgets.QLabel):
                start = None
                def mousePressEvent(self, event):
                    self.start = event.position()
                def mouseReleaseEvent(self, event):
                    if self.start is not None and (event.position() - self.start).manhattanLength() > 10:
                        update(drags=state["drags"] + 1)
                    self.start = None
                def wheelEvent(self, event):
                    update(scrolls=state["scrolls"] + 1)
                    event.accept()
            surface = Surface("Scroll here; drag here")
            surface.setMinimumSize(300, 120)
            box.addWidget(surface)
        window.show()
        update()
        app.exec()


if __name__ == "__main__":
    main()
