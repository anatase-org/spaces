"""Host process and logind-session verification."""

from __future__ import annotations

import ctypes
import ctypes.util
import os
from functools import cache
from pathlib import Path

from .. import _
from .. import core
from .protocol import valid_host_session_id


@cache
def _libraries() -> tuple[ctypes.CDLL, ctypes.CDLL]:
    systemd_name = ctypes.util.find_library("systemd") or "libsystemd.so.0"
    systemd = ctypes.CDLL(systemd_name)
    libc = ctypes.CDLL(None)
    systemd.sd_pid_get_session.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    systemd.sd_pid_get_session.restype = ctypes.c_int
    systemd.sd_session_get_uid.argtypes = [
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_uint),
    ]
    systemd.sd_session_get_uid.restype = ctypes.c_int
    libc.free.argtypes = [ctypes.c_void_p]
    return systemd, libc


def _proc_stat(pid: int) -> list[str]:
    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError as error:
        raise core.SpacesError(
            _("Could not inspect process {pid}: {error}", pid=pid, error=error)
        ) from error
    end = value.rfind(")")
    if end < 0:
        raise core.SpacesError(_("Process {pid} has invalid status.", pid=pid))
    return value[end + 2 :].split()


def process_start_time(pid: int) -> int:
    fields = _proc_stat(pid)
    if len(fields) <= 19:
        raise core.SpacesError(_("Process {pid} has invalid status.", pid=pid))
    try:
        return int(fields[19])
    except ValueError as error:
        raise core.SpacesError(
            _("Process {pid} has an invalid start time.", pid=pid)
        ) from error


def process_parent(pid: int) -> int:
    fields = _proc_stat(pid)
    if len(fields) < 2:
        raise core.SpacesError(_("Process {pid} has invalid status.", pid=pid))
    try:
        return int(fields[1])
    except ValueError as error:
        raise core.SpacesError(
            _("Process {pid} has an invalid parent.", pid=pid)
        ) from error


def cgroup_components(pid: int) -> set[str]:
    value = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
    return {
        component
        for line in value.splitlines()
        for component in line.partition(":")[2].split("/")
        if component
    }


def session_for_pid(pid: int) -> str:
    systemd, libc = _libraries()
    value = ctypes.c_void_p()
    result = systemd.sd_pid_get_session(pid, ctypes.byref(value))
    if result < 0:
        raise core.SpacesError(
            _("Process {pid} does not belong to a host login session.", pid=pid)
        )
    try:
        return ctypes.string_at(value).decode()
    finally:
        libc.free(value)


def session_uid(session_id: str) -> int:
    systemd, _libc = _libraries()
    uid = ctypes.c_uint()
    result = systemd.sd_session_get_uid(
        session_id.encode(), ctypes.byref(uid)
    )
    if result < 0:
        raise core.SpacesError(
            _("Host login session {session!r} is unavailable.", session=session_id)
        )
    return uid.value


def client_session(pid: int) -> str:
    """Resolve a login session, including systemd user-manager applications."""

    try:
        return session_for_pid(pid)
    except core.SpacesError:
        session_id = os.environ.get("XDG_SESSION_ID", "")
        if not valid_host_session_id(session_id):
            raise
        return session_id


def process_session_matches(pid: int, session_id: str, uid: int) -> bool:
    """Verify direct membership or an inherited user-manager session hint."""

    if session_uid(session_id) != uid:
        return False
    try:
        return session_for_pid(pid) == session_id
    except core.SpacesError:
        try:
            environment = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
            components = cgroup_components(pid)
        except OSError:
            return False
        return (
            f"XDG_SESSION_ID={session_id}".encode() in environment
            and f"user@{uid}.service" in components
        )


def session_process(session_id: str, uid: int) -> tuple[int, int]:
    """Find a live process owned by UID inside the exact logind session."""

    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        pid = int(entry.name)
        try:
            if entry.stat().st_uid != uid:
                continue
            if session_for_pid(pid) != session_id:
                continue
            return pid, process_start_time(pid)
        except (OSError, core.SpacesError):
            continue
    raise core.SpacesError(
        _(
            "No live process for UID {uid} remains in host session "
            "{session!r}.",
            uid=uid,
            session=session_id,
        )
    )
