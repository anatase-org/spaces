"""Fedora Linux distribution driver."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .. import _
from .model import Distribution, DistributionError
from .mounts import hidden_selinuxfs, mounted_api_filesystems
from .pam import SPACES_PAM_BLOCK, atomic_write, safe_directory, safe_file


PACKAGES = (
    "fedora-release-container",
    "fedora-repos",
    "systemd",
    "systemd-pam",
    "dnf5",
    "passwd",
    "openssh-clients",
    "git",
    "nano",
    "sudo",
    "dbus-tools",
    # auth
    "polkit",
    "polkit-kde",
    # desktop integration
    "plasma-breeze",
    "plasma-integration",
    "kde-cli-tools",
    "kf6-kwallet",
    "qca-qt6-ossl",
    "qt6-qtwayland",
    "file",
    "pipewire",
    "xdg-desktop-portal",
    "xdg-desktop-portal-kde",
)
RELEASES = {"44": _("44")}
HOST_REPOSITORY_DIRECTORY = Path("/usr/share/spaces/repos")
AUTHSELECT_STATE = Path("var/lib/spaces/fedora-authselect.json")
AUTHSELECT_PROFILE = Path("etc/authselect/custom/spaces")
AUTHSELECT_FILES = ("system-auth", "password-auth")


def _authselect(rootfs: Path, *arguments: str, capture: bool = False) -> str:
    completed = subprocess.run(
        ["chroot", str(rootfs), "/usr/bin/authselect", *arguments],
        check=True,
        capture_output=capture,
        text=capture,
    )
    return completed.stdout.strip() if capture else ""


def _remove_profile(rootfs: Path) -> None:
    profile = rootfs / AUTHSELECT_PROFILE
    try:
        profile.lstat()
    except FileNotFoundError:
        return
    directory = safe_directory(rootfs, AUTHSELECT_PROFILE, "Fedora authselect profile")
    shutil.rmtree(directory)


def _read_authselect_state(rootfs: Path) -> list[str] | None:
    state_path = rootfs / AUTHSELECT_STATE
    try:
        state_path.lstat()
    except FileNotFoundError:
        return None
    state_path = safe_file(
        rootfs,
        AUTHSELECT_STATE,
        "Fedora authselect state",
    )
    try:
        value = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DistributionError(_("Invalid Fedora authselect state.")) from error
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("selection"), list)
        or not value["selection"]
        or not all(isinstance(item, str) and item for item in value["selection"])
    ):
        raise DistributionError(_("Invalid Fedora authselect state."))
    return value["selection"]


def _write_authselect_state(rootfs: Path, selection: list[str]) -> None:
    directory = rootfs / AUTHSELECT_STATE.parent
    safe_directory(
        rootfs,
        AUTHSELECT_STATE.parent.parent,
        "Fedora state parent",
    )
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise DistributionError(_("Unsafe Fedora Spaces state directory."))
    directory.mkdir(parents=True, exist_ok=True)
    safe_directory(
        rootfs,
        AUTHSELECT_STATE.parent,
        "Fedora Spaces state",
    )
    atomic_write(
        rootfs / AUTHSELECT_STATE,
        (json.dumps({"selection": selection}) + "\n").encode("utf-8"),
        0o600,
    )


def _inject_authselect_profile(rootfs: Path) -> None:
    for name in AUTHSELECT_FILES:
        relative = AUTHSELECT_PROFILE / name
        path = safe_file(rootfs, relative, f"Fedora authselect {name}")
        content = path.read_text(encoding="utf-8")
        if content.count(SPACES_PAM_BLOCK) > 1:
            raise DistributionError(_("Invalid Fedora authselect profile."))
        if SPACES_PAM_BLOCK in content:
            continue
        lines = content.splitlines(keepends=True)
        insertion = 1 if lines and lines[0].startswith("#%PAM-") else 0
        lines.insert(insertion, SPACES_PAM_BLOCK)
        atomic_write(
            path,
            "".join(lines).encode("utf-8"),
            path.stat().st_mode & 0o777,
        )


def _enable_authselect(rootfs: Path) -> None:
    selection = _read_authselect_state(rootfs)
    created = False
    if selection is None:
        current = _authselect(rootfs, "current", "--raw", capture=True).split()
        if not current or current[0] == "custom/spaces":
            raise DistributionError(
                _("Could not determine the Fedora authselect profile.")
            )
        selection = current
        _authselect(rootfs, "create-profile", "spaces", "-b", selection[0])
        created = True
        try:
            _inject_authselect_profile(rootfs)
            _write_authselect_state(rootfs, selection)
        except BaseException:
            _remove_profile(rootfs)
            raise
    else:
        _inject_authselect_profile(rootfs)

    try:
        _authselect(rootfs, "select", "custom/spaces", *selection[1:], "--force")
    except (OSError, subprocess.CalledProcessError) as error:
        if created:
            try:
                _authselect(rootfs, "select", *selection, "--force")
            except (OSError, subprocess.CalledProcessError):
                pass
            (rootfs / AUTHSELECT_STATE).unlink(missing_ok=True)
            _remove_profile(rootfs)
        raise DistributionError(
            _("Could not enable Fedora host authentication.")
        ) from error


def _disable_authselect(rootfs: Path) -> None:
    selection = _read_authselect_state(rootfs)
    if selection is None:
        return
    try:
        _authselect(rootfs, "select", *selection, "--force")
    except (OSError, subprocess.CalledProcessError) as error:
        raise DistributionError(
            _("Could not disable Fedora host authentication.")
        ) from error
    _remove_profile(rootfs)
    (rootfs / AUTHSELECT_STATE).unlink()


class FedoraDistribution(Distribution):
    def describe(self, metadata: Mapping[str, Any]) -> str:
        self.validate(metadata)
        return _("Fedora {version}", version=metadata["version"])

    def command(self, metadata: Mapping[str, Any], rootfs: Path) -> list[str]:
        self.validate(metadata)
        version = str(metadata["version"])
        return [
            "dnf5",
            "--assumeyes",
            f"--installroot={rootfs}",
            f"--releasever={version}",
            "--setopt=install_weak_deps=False",
            "--setopt=tsflags=nocontexts",
            f"--setopt=reposdir={HOST_REPOSITORY_DIRECTORY}",
            "install",
            *PACKAGES,
        ]

    def bootstrap(self, metadata: Mapping[str, Any], rootfs: Path) -> None:
        version = str(metadata["version"])
        print(
            _("Bootstrapping Fedora {version}...", version=version),
            flush=True,
        )
        with mounted_api_filesystems(rootfs, "Fedora"):
            with hidden_selinuxfs():
                subprocess.run(self.command(metadata, rootfs), check=True)

    def reconcile_host_authentication(
        self,
        rootfs: Path,
        enabled: bool,
    ) -> bool:
        if enabled:
            _enable_authselect(rootfs)
        else:
            _disable_authselect(rootfs)
        return True


DISTRIBUTION = FedoraDistribution(
    id="fedora",
    default_name="fedora",
    configuration_title=_("Fedora version"),
    configuration_description=_("Choose the Fedora release to bootstrap."),
    option_key="version",
    configuration_options=RELEASES,
    default_option="44",
)
