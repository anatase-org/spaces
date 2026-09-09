"""Per-space host system D-Bus broker and guest activation mounts."""

from __future__ import annotations

import logging
import stat
import subprocess
import threading
import time
from pathlib import Path

from . import _, core

logger = logging.getLogger(__name__)
RUNTIME_ROOT = Path("/run/spaces")
DATA_ROOT = Path("/usr/share/spaces/system-bridge")
BROKER = Path("/usr/lib/spaces/spaces-system-broker")
GUEST_RUNTIME = "/run/spaces-host/system"
SERVICES = (
    "org.freedesktop.resolve1",
    "org.freedesktop.NetworkManager",
    "org.freedesktop.UPower",
)
GUEST_DAEMONS = (
    "systemd-resolved.service",
    "NetworkManager.service",
    "upower.service",
)


def guest_bind_arguments(directory: Path) -> tuple[str, ...]:
    unit = DATA_ROOT / "spaces-system-broker.service"
    bindings = [
        f"--bind-ro={directory}:{GUEST_RUNTIME}",
        f"--bind-ro={unit}:/etc/systemd/system/spaces-system-broker.service",
        f"--bind-ro={DATA_ROOT / 'multi-user.conf'}:"
        "/etc/systemd/system/multi-user.target.d/50-spaces-system-broker.conf",
        f"--bind-ro={DATA_ROOT / 'system.conf'}:/etc/dbus-1/system.d/zz-spaces-system.conf",
    ]
    # Binding onto a unit alias follows its symlink into /usr, making the
    # package-owned unit a mount point that package managers cannot replace.
    # Conditions also let package scripts start/restart the unit successfully
    # without launching a guest daemon that competes with the host bridge.
    for name in (*GUEST_DAEMONS, *(f"dbus-{name}.service" for name in SERVICES)):
        bindings.append(
            f"--bind-ro={DATA_ROOT / 'host-service.conf'}:"
            f"/etc/systemd/system/{name}.d/50-spaces-host-service.conf"
        )
    for name in SERVICES:
        # The system bus searches /etc before package-owned service directories.
        bindings.append(
            f"--bind-ro={DATA_ROOT}/dbus-1/system-services/{name}.service:"
            f"/etc/dbus-1/system-services/{name}.service"
        )
    return tuple(bindings)


class SystemBusService:
    """Keep the broker alive until the nspawn supervisor tears the space down."""

    def __init__(self, space_name: str, network: str) -> None:
        core.validate_space_name(space_name)
        if network not in core.NETWORK_LEVELS:
            raise core.SpacesError(_("Invalid system bridge network permission."))
        self.directory = RUNTIME_ROOT / space_name / "system-bus"
        self.socket = self.directory / "bus.sock"
        self.network = network
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def bind_arguments(self) -> tuple[str, ...]:
        return guest_bind_arguments(self.directory)

    def _spawn(self) -> None:
        self.socket.unlink(missing_ok=True)
        command = [str(BROKER), "--broker", str(self.socket)]
        if self.network == "admin":
            command.append("--admin")
        self._process = subprocess.Popen(command, stdin=subprocess.DEVNULL)

    def start(self) -> None:
        for path in (self.directory.parent, self.directory):
            if path.is_symlink():
                raise core.SpacesError(_("Unsafe system bridge runtime directory."))
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = path.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0:
                raise core.SpacesError(_("System bridge runtime must belong to root."))
            path.chmod(0o700)
        try:
            self._spawn()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    raise core.SpacesError(_("System bridge exited before readiness."))
                try:
                    metadata = self.socket.lstat()
                    if stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == 0:
                        break
                except FileNotFoundError:
                    pass
                time.sleep(0.02)
            else:
                raise core.SpacesError(_("Timed out starting system bridge."))
            self._thread = threading.Thread(
                target=self._supervise, name="spaces-system-bus"
            )
            self._thread.start()
        except Exception:
            self.stop()
            raise

    def _supervise(self) -> None:
        while not self._stop.wait(1):
            if self._process is not None and self._process.poll() is None:
                continue
            logger.warning(_("Restarting the host system bridge."))
            try:
                self._spawn()
            except OSError as error:
                logger.error(_("Could not restart system bridge: {error}", error=error))

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        if self._process is not None:
            if self._process.poll() is None:
                self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
            self._process = None
        self.socket.unlink(missing_ok=True)
