"""Guest probe using stock libatspi via ctypes; no Python GI dependency.

Only acts on the uniquely named manual_atspi_fixture.py window. The host
fixture's JSON state is the independent action oracle.
"""

from __future__ import annotations

import argparse
import ctypes as C
import json
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("token")
    parser.add_argument("--expect-absent", action="store_true")
    args = parser.parse_args()
    atspi = C.CDLL("libatspi.so.0")
    glib = C.CDLL("libglib-2.0.so.0")

    def function(name, result, *arguments):
        func = getattr(atspi, name)
        func.restype, func.argtypes = result, list(arguments)
        return func

    init = function("atspi_init", C.c_int)
    desktop = function("atspi_get_desktop", C.c_void_p, C.c_int)
    count = function(
        "atspi_accessible_get_child_count", C.c_int, C.c_void_p, C.c_void_p
    )
    child = function(
        "atspi_accessible_get_child_at_index",
        C.c_void_p,
        C.c_void_p,
        C.c_int,
        C.c_void_p,
    )
    get_name = function(
        "atspi_accessible_get_name", C.c_void_p, C.c_void_p, C.c_void_p
    )
    get_action = function(
        "atspi_accessible_get_action_iface", C.c_void_p, C.c_void_p
    )
    action = function(
        "atspi_action_do_action", C.c_int, C.c_void_p, C.c_int, C.c_void_p
    )
    get_editable = function(
        "atspi_accessible_get_editable_text_iface", C.c_void_p, C.c_void_p
    )
    edit = function(
        "atspi_editable_text_set_text_contents",
        C.c_int,
        C.c_void_p,
        C.c_char_p,
        C.c_void_p,
    )
    glib.g_free.argtypes = [C.c_void_p]
    glib.g_main_context_iteration.argtypes = [C.c_void_p, C.c_int]
    glib.g_main_context_iteration.restype = C.c_int
    # Bound calls even if an unrelated desktop application is unresponsive.
    function("atspi_set_timeout", None, C.c_int, C.c_int)(1000, 3000)
    assert init() <= 1, "libatspi initialization failed"

    def name(obj):
        pointer = get_name(obj, None)
        if not pointer:
            return ""
        try:
            return C.string_at(pointer).decode("utf-8")
        finally:
            glib.g_free(pointer)

    def walk(obj, depth):
        if not obj or depth < 0:
            return
        yield obj
        for index in range(min(max(count(obj, None), 0), 200)):
            yield from walk(child(obj, index, None), depth - 1)

    title = "Spaces AT-SPI fixture " + args.token
    fixture = None
    deadline = time.monotonic() + 8
    while fixture is None and time.monotonic() < deadline:
        fixture = next(
            (obj for obj in walk(desktop(0), 2) if name(obj) == title), None
        )
        if fixture is None:
            time.sleep(0.2)
    if args.expect_absent:
        assert fixture is None, "host fixture is still accessible"
        print(json.dumps({"host_fixture_absent": True}))
        return
    assert (
        fixture is not None
    ), "host fixture missing from guest accessibility tree"
    widgets = {name(obj): obj for obj in walk(fixture, 5)}
    button = widgets["Spaces fixture button"]
    entry = widgets["Spaces fixture entry"]
    checkbox = widgets["Spaces fixture checkbox"]
    events = []
    callback_type = C.CFUNCTYPE(None, C.c_void_p, C.c_void_p)
    callback = callback_type(lambda *_: events.append(True))
    listener = function(
        "atspi_event_listener_new",
        C.c_void_p,
        callback_type,
        C.c_void_p,
        C.c_void_p,
    )(callback, None, None)
    register = function(
        "atspi_event_listener_register",
        C.c_int,
        C.c_void_p,
        C.c_char_p,
        C.c_void_p,
    )
    assert register(
        listener, b"object:state-changed", None
    ), "event subscription failed"
    assert action(get_action(button), 0, None), "button action failed"
    assert edit(
        get_editable(entry), "Héllo 世界".encode(), None
    ), "text editing failed"
    assert action(get_action(checkbox), 0, None), "checkbox action failed"
    deadline = time.monotonic() + 3
    while not events and time.monotonic() < deadline:
        glib.g_main_context_iteration(None, False)
        time.sleep(0.01)
    assert events, "no AT-SPI state-change events received"
    print(
        json.dumps(
            {
                "fixture": title,
                "actions": 2,
                "text": "Héllo 世界",
                "events": len(events),
            }
        )
    )
    function(
        "atspi_event_listener_deregister",
        C.c_int,
        C.c_void_p,
        C.c_char_p,
        C.c_void_p,
    )(listener, b"object:state-changed", None)
    function("atspi_exit", C.c_int)()


if __name__ == "__main__":
    main()
