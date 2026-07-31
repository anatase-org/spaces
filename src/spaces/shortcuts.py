"""Export safe desktop application entries from a space to the host."""

from __future__ import annotations

import ctypes
import hashlib
import io
import logging
import os
import re
import select
import shutil
import stat
import struct
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from importlib import resources
from pathlib import Path, PurePosixPath

from PIL import Image, UnidentifiedImageError

from . import _

SHORTCUT_VERSION = 1
APPLICATIONS_ROOT = Path("/usr/local/share/applications")
ICON_ROOT_NAME = "spaces-icons"
SOURCE_DIRECTORIES = (
    ("system", PurePosixPath("/usr/share/applications"), ""),
    ("local", PurePosixPath("/usr/local/share/applications"), "local-"),
)
BLACKLIST = frozenset(
    {
        "systemsettings",
        "kdesystemsettings",
    }
)
BLACKLIST_TERMINAL = True
MAX_DESKTOP_FILE_SIZE = 1024 * 1024
MAX_ICON_FILE_SIZE = 32 * 1024 * 1024
MAX_ICON_PIXELS = 16 * 1024 * 1024
ICON_SIZE = 256
ICON_EXTENSIONS = frozenset(
    {".png", ".svg", ".xpm", ".jpg", ".jpeg", ".ico", ".webp", ".bmp"}
)
SVG_CONVERTER = Path("/usr/bin/rsvg-convert")
SVG_RENDER_TIMEOUT = 5
MAIN_KEYS = frozenset(
    {
        "Type",
        "Version",
        "Name",
        "GenericName",
        "Comment",
        "Keywords",
        "Exec",
        "Icon",
        "Terminal",
        "Categories",
        "MimeType",
        "Actions",
        "OnlyShowIn",
        "NotShowIn",
        "NoDisplay",
        "Hidden",
        "StartupNotify",
        "StartupWMClass",
        "PrefersNonDefaultGPU",
        "SingleMainWindow",
    }
)
LOCALIZED_KEYS = frozenset({"Name", "GenericName", "Comment", "Keywords"})
ACTION_KEYS = frozenset({"Name", "Exec", "Icon"})
KEY_PATTERN = re.compile(r"^[A-Za-z0-9-]+(?:\[[^\]\r\n]+\])?$")
ACTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")
DESKTOP_ID_UNSAFE_PATTERN = re.compile(r"[^A-Za-z0-9._-]")
INOTIFY_EVENT = struct.Struct("iIII")
IN_MODIFY = 0x00000002
IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_IGNORED = 0x00008000
IN_Q_OVERFLOW = 0x00004000
WATCH_MASK = (
    IN_MODIFY
    | IN_ATTRIB
    | IN_CLOSE_WRITE
    | IN_MOVED_FROM
    | IN_MOVED_TO
    | IN_CREATE
    | IN_DELETE
    | IN_DELETE_SELF
    | IN_MOVE_SELF
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DesktopSource:
    origin: str
    path: Path
    relative: PurePosixPath
    output_name: str


@dataclass(frozen=True)
class GeneratedShortcut:
    name: str
    desktop: bytes
    icon_name: str | None
    icon: bytes | None


def _normalized_id(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _is_blacklisted(relative: PurePosixPath) -> bool:
    desktop_id = str(relative).replace("/", "-")
    return (
        _normalized_id(relative.stem) in BLACKLIST
        or _normalized_id(desktop_id.removesuffix(".desktop")) in BLACKLIST
    )


def _output_id(relative: PurePosixPath, prefix: str) -> str:
    raw = prefix + str(relative).replace("/", "-")
    sanitized = DESKTOP_ID_UNSAFE_PATTERN.sub("_", raw)
    if sanitized != raw or len(sanitized.encode("utf-8")) > 240:
        digest = hashlib.sha256(os.fsencode(raw)).hexdigest()[:10]
        stem = sanitized.removesuffix(".desktop")
        stem = stem.encode("utf-8")[:220].decode("utf-8", errors="ignore")
        sanitized = f"{stem}-{digest}.desktop"
    return sanitized


def _shortcut_prefix(space_name: str, version: int = SHORTCUT_VERSION) -> str:
    return f"spaces-{space_name}-v{version}-"


def _shortcut_name(space_name: str, source_name: str) -> str:
    prefix = _shortcut_prefix(space_name)
    raw = prefix + source_name
    if len(raw.encode("utf-8")) <= 240:
        return raw

    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    suffix = f"-{digest}.desktop"
    budget = 240 - len(prefix.encode("utf-8")) - len(suffix.encode("utf-8"))
    stem = source_name.removesuffix(".desktop")
    stem = stem.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
    return f"{prefix}{stem}{suffix}"


def _shortcut_version(name: str, space_name: str, suffix: str) -> int | None:
    prefix = f"spaces-{space_name}-v"
    if not name.startswith(prefix) or not name.endswith(suffix):
        return None
    version, separator, remainder = name[len(prefix) :].partition("-")
    if not separator or not version.isdigit() or not remainder:
        return None
    return int(version)


def _safe_source_directory(rootfs: Path, guest_path: PurePosixPath) -> Path | None:
    candidate = rootfs.joinpath(*guest_path.parts[1:])
    try:
        fixed_root = rootfs.resolve(strict=True)
        fixed_candidate = candidate.resolve(strict=True)
    except OSError:
        return None
    if not fixed_candidate.is_relative_to(fixed_root) or not fixed_candidate.is_dir():
        return None
    return fixed_candidate


def _desktop_sources(rootfs: Path) -> list[DesktopSource]:
    candidates: list[DesktopSource] = []
    used_names: dict[str, str] = {}
    for origin, guest_directory, prefix in SOURCE_DIRECTORIES:
        directory = _safe_source_directory(rootfs, guest_directory)
        if directory is None:
            continue
        for path in sorted(directory.rglob("*.desktop")):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                relative_path = path.relative_to(directory)
            except (OSError, ValueError):
                continue
            relative = PurePosixPath(relative_path.as_posix())
            if _is_blacklisted(relative):
                continue
            output_name = _output_id(relative, prefix)
            identity = f"{origin}/{relative}"
            previous = used_names.get(output_name)
            if previous is not None and previous != identity:
                stem = output_name.removesuffix(".desktop")
                digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
                output_name = f"{stem}-{digest}.desktop"
            used_names[output_name] = identity
            candidates.append(DesktopSource(origin, path, relative, output_name))
    return candidates


def _read_groups(path: Path) -> list[tuple[str, dict[str, str]]] | None:
    try:
        if path.stat().st_size > MAX_DESKTOP_FILE_SIZE:
            return None
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    groups: list[tuple[str, dict[str, str]]] = []
    current: dict[str, str] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1]
            if not name or "\0" in name:
                return None
            current = {}
            groups.append((name, current))
            continue
        if current is None or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        key = key.strip()
        if not KEY_PATTERN.fullmatch(key) or "\0" in value:
            continue
        current[key] = value
    return groups


def _base_key(key: str) -> str:
    return key.partition("[")[0]


def _is_allowed_key(key: str, allowed: frozenset[str]) -> bool:
    base = _base_key(key)
    return key in allowed or (base in LOCALIZED_KEYS and key.endswith("]"))


def _wrap_exec(space_name: str, command: str) -> str:
    return f"/usr/bin/spaces enter --graphical {space_name} -- " f"{command}"


def _render_groups(
    groups: list[tuple[str, dict[str, str]]],
    space_name: str,
    icon_path: str | None,
    startup_wm_class: str,
) -> bytes | None:
    main = next((values for name, values in groups if name == "Desktop Entry"), None)
    if (
        main is None
        or main.get("Type") != "Application"
        or not main.get("Name", "").strip()
        or not main.get("Exec", "").strip()
    ):
        return None

    action_names = {value for value in main.get("Actions", "").split(";") if value}
    valid_actions: list[tuple[str, dict[str, str]]] = []
    for group_name, values in groups:
        prefix = "Desktop Action "
        if not group_name.startswith(prefix):
            continue
        action_name = group_name.removeprefix(prefix)
        if (
            ACTION_ID_PATTERN.fullmatch(action_name)
            and action_name in action_names
            and values.get("Exec", "").strip()
        ):
            valid_actions.append((group_name, values))
    valid_action_names = {
        name.removeprefix("Desktop Action ") for name, _values in valid_actions
    }

    output: list[str] = ["[Desktop Entry]"]
    for key, value in main.items():
        if not _is_allowed_key(key, MAIN_KEYS):
            continue
        base = _base_key(key)
        if base == "Name":
            value = f"{value} ({space_name})"
        elif key == "Exec":
            value = _wrap_exec(space_name, value)
        elif key == "Icon":
            if icon_path is None:
                continue
            value = icon_path
        elif key == "StartupWMClass":
            value = startup_wm_class
        elif key == "Actions":
            value = "".join(
                f"{action};"
                for action in main.get("Actions", "").split(";")
                if action in valid_action_names
            )
            if not value:
                continue
        output.append(f"{key}={value}")
    if "Icon" not in main and icon_path is not None:
        output.append(f"Icon={icon_path}")
    if "StartupWMClass" not in main:
        output.append(f"StartupWMClass={startup_wm_class}")
    output.append("DBusActivatable=false")

    for group_name, values in valid_actions:
        output.extend(("", f"[{group_name}]"))
        for key, value in values.items():
            if not _is_allowed_key(key, ACTION_KEYS):
                continue
            if key == "Exec":
                value = _wrap_exec(space_name, value)
            elif key == "Icon":
                if icon_path is None:
                    continue
                value = icon_path
            output.append(f"{key}={value}")
    return ("\n".join(output) + "\n").encode("utf-8")


def _safe_icon_candidate(rootfs: Path, candidate: Path) -> Path | None:
    try:
        fixed_root = rootfs.resolve(strict=True)
        fixed_candidate = candidate.resolve(strict=True)
        metadata = fixed_candidate.stat()
    except OSError:
        return None
    if (
        not fixed_candidate.is_relative_to(fixed_root)
        or not fixed_candidate.is_file()
        or metadata.st_size > MAX_ICON_FILE_SIZE
        or fixed_candidate.suffix.casefold() not in ICON_EXTENSIONS
    ):
        return None
    return fixed_candidate


def _icon_key(value: str) -> str:
    """Keep reverse-DNS icon names intact while removing real file extensions."""
    path = Path(value)
    if path.suffix.casefold() in ICON_EXTENSIONS:
        return path.stem
    return path.name


def _build_icon_index(
    rootfs: Path,
    values: set[str],
) -> dict[str, list[Path]]:
    requested = {
        _icon_key(value)
        for value in values
        if value and not value.startswith("/") and "\0" not in value
    }
    index = {name: [] for name in requested}
    if not requested:
        return index
    for guest_root in (
        "/usr/local/share/icons",
        "/usr/share/icons",
        "/usr/local/share/pixmaps",
        "/usr/share/pixmaps",
    ):
        directory = _safe_source_directory(rootfs, PurePosixPath(guest_root))
        if directory is None:
            continue
        try:
            for parent, directories, files in os.walk(directory, followlinks=False):
                parent_path = Path(parent)
                directories[:] = [
                    name
                    for name in directories
                    if not (parent_path / name).is_symlink()
                ]
                for name in files:
                    path = parent_path / name
                    stem = path.stem
                    if stem not in requested:
                        continue
                    candidate = _safe_icon_candidate(rootfs, path)
                    if candidate is not None:
                        index[stem].append(candidate)
        except OSError:
            continue
    return index


def _icon_candidates(
    rootfs: Path,
    value: str,
    index: dict[str, list[Path]],
) -> list[Path]:
    if not value or "\0" in value:
        return []
    if value.startswith("/"):
        candidate = _safe_icon_candidate(rootfs, rootfs / value.lstrip("/"))
        return [candidate] if candidate is not None else []

    return index.get(_icon_key(value), [])


def _load_icon(
    rootfs: Path,
    value: str,
    index: dict[str, list[Path]],
) -> Image.Image | None:
    ranked: list[tuple[int, int, Path, Image.Image]] = []
    for candidate in _icon_candidates(rootfs, value, index):
        suffix = candidate.suffix.casefold()
        if suffix == ".svg":
            try:
                rendered = subprocess.run(
                    [
                        SVG_CONVERTER,
                        "--format=png",
                        f"--width={ICON_SIZE}",
                        f"--height={ICON_SIZE}",
                        "--keep-aspect-ratio",
                        "--",
                        candidate,
                    ],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=SVG_RENDER_TIMEOUT,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if (
                rendered.returncode != 0
                or not rendered.stdout
                or len(rendered.stdout) > MAX_ICON_FILE_SIZE
            ):
                continue
            stream: Path | io.BytesIO = io.BytesIO(rendered.stdout)
        else:
            stream = candidate
        try:
            with Image.open(stream) as source:
                width, height = source.size
                pixels = width * height
                if width <= 0 or height <= 0 or pixels > MAX_ICON_PIXELS:
                    continue
                image = source.convert("RGBA")
        except (
            OSError,
            ValueError,
            KeyError,
            UnidentifiedImageError,
            Image.DecompressionBombError,
        ):
            continue
        preference = 2 if suffix == ".svg" else (1 if suffix == ".png" else 0)
        ranked.append((preference, pixels, candidate, image))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1], str(item[2])))
    return ranked[-1][3]


def _load_overlay(distribution_id: str) -> Image.Image | None:
    try:
        resource = resources.files("spaces").joinpath(
            "overlay", f"{distribution_id}.png"
        )
        with resource.open("rb") as stream, Image.open(stream) as source:
            return source.convert("RGBA")
    except (FileNotFoundError, OSError, UnidentifiedImageError):
        return None


def _render_icon(
    rootfs: Path,
    source_value: str,
    overlay_image: Image.Image | None,
    index: dict[str, list[Path]],
) -> bytes | None:
    source = _load_icon(rootfs, source_value, index)
    overlay = overlay_image.copy() if overlay_image is not None else None
    if source is None and overlay is None:
        return None

    canvas = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    if source is not None:
        source.thumbnail((ICON_SIZE, ICON_SIZE), Image.Resampling.LANCZOS)
        canvas.alpha_composite(
            source,
            ((ICON_SIZE - source.width) // 2, (ICON_SIZE - source.height) // 2),
        )
    if overlay is not None:
        overlay.thumbnail((ICON_SIZE, ICON_SIZE), Image.Resampling.LANCZOS)
        if source is None:
            bounding = overlay.getbbox()
            if bounding is not None:
                badge = overlay.crop(bounding)
                badge.thumbnail((192, 192), Image.Resampling.LANCZOS)
                canvas.alpha_composite(
                    badge,
                    ((ICON_SIZE - badge.width) // 2, (ICON_SIZE - badge.height) // 2),
                )
        else:
            canvas.alpha_composite(overlay, (0, 0))
    output = io.BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()


def generate(
    space_name: str,
    rootfs: Path,
    distribution_id: str,
    applications_root: Path | None = None,
) -> list[GeneratedShortcut]:
    if applications_root is None:
        applications_root = APPLICATIONS_ROOT
    icon_directory = applications_root / ICON_ROOT_NAME
    generated: list[GeneratedShortcut] = []
    parsed: list[
        tuple[DesktopSource, list[tuple[str, dict[str, str]]], dict[str, str]]
    ] = []
    for source in _desktop_sources(rootfs):
        groups = _read_groups(source.path)
        if groups is None:
            continue
        main = next(
            (values for name, values in groups if name == "Desktop Entry"),
            {},
        )
        if (
            BLACKLIST_TERMINAL
            and main.get("Terminal", "").strip().casefold() == "true"
        ):
            continue
        parsed.append((source, groups, main))
    icon_index = _build_icon_index(
        rootfs,
        {main.get("Icon", "") for _source, _groups, main in parsed},
    )
    overlay = _load_overlay(distribution_id)
    for source, groups, main in parsed:
        shortcut_name = _shortcut_name(space_name, source.output_name)
        icon_bytes = _render_icon(
            rootfs,
            main.get("Icon", ""),
            overlay,
            icon_index,
        )
        icon_name = (
            f"{shortcut_name.removesuffix('.desktop')}.png"
            if icon_bytes is not None
            else None
        )
        icon_path = str(icon_directory / icon_name) if icon_name else None
        startup_wm_class = main.get("StartupWMClass", "").strip()
        if not startup_wm_class:
            startup_wm_class = source.relative.stem
        desktop = _render_groups(
            groups,
            space_name,
            icon_path,
            startup_wm_class,
        )
        if desktop is None:
            continue
        generated.append(
            GeneratedShortcut(
                shortcut_name,
                desktop,
                icon_name,
                icon_bytes,
            )
        )
    return generated


def _ensure_directory(path: Path) -> None:
    path.mkdir(mode=0o755, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise OSError(f"Unsafe shortcut directory: {path}")
    if stat.S_IMODE(path.stat().st_mode) != 0o755:
        path.chmod(0o755)


def _atomic_write(path: Path, content: bytes) -> None:
    try:
        metadata = path.lstat()
        if stat.S_ISREG(metadata.st_mode) and path.read_bytes() == content:
            if stat.S_IMODE(metadata.st_mode) != 0o644:
                path.chmod(0o644)
            return
    except OSError:
        pass

    temporary_path: Path | None = None
    descriptor, temporary_name = tempfile.mkstemp(prefix=".spaces-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fchmod(output.fileno(), 0o644)
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _remove_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path)


def cleanup_versions(
    space_name: str,
    *,
    applications_root: Path | None = None,
    keep_current: bool = True,
) -> None:
    if applications_root is None:
        applications_root = APPLICATIONS_ROOT
    if applications_root.exists():
        for candidate in applications_root.iterdir():
            version = _shortcut_version(candidate.name, space_name, ".desktop")
            if version is None:
                continue
            if keep_current and version == SHORTCUT_VERSION:
                continue
            _remove_path(candidate)

    icons = applications_root / ICON_ROOT_NAME
    if icons.exists():
        if icons.is_symlink() or not icons.is_dir():
            raise OSError(f"Unsafe shortcut icon root: {icons}")
        for candidate in icons.iterdir():
            version = _shortcut_version(candidate.name, space_name, ".png")
            if version is None:
                continue
            if keep_current and version == SHORTCUT_VERSION:
                continue
            _remove_path(candidate)


def remove(
    space_name: str,
    *,
    applications_root: Path | None = None,
) -> None:
    if applications_root is None:
        applications_root = APPLICATIONS_ROOT
    cleanup_versions(
        space_name,
        applications_root=applications_root,
        keep_current=False,
    )
    icons = applications_root / ICON_ROOT_NAME
    if icons.exists() and icons.is_dir() and not icons.is_symlink():
        try:
            icons.rmdir()
        except OSError:
            pass


def reconcile(
    space_name: str,
    rootfs: Path,
    distribution_id: str,
    *,
    applications_root: Path | None = None,
) -> None:
    if applications_root is None:
        applications_root = APPLICATIONS_ROOT
    icons = applications_root / ICON_ROOT_NAME
    cleanup_versions(space_name, applications_root=applications_root)
    _ensure_directory(icons)

    expected_desktops: set[str] = set()
    expected_icons: set[str] = set()
    for shortcut in generate(
        space_name,
        rootfs,
        distribution_id,
        applications_root,
    ):
        _atomic_write(applications_root / shortcut.name, shortcut.desktop)
        expected_desktops.add(shortcut.name)
        if shortcut.icon_name is not None and shortcut.icon is not None:
            _atomic_write(icons / shortcut.icon_name, shortcut.icon)
            expected_icons.add(shortcut.icon_name)

    for candidate in applications_root.iterdir():
        if (
            _shortcut_version(candidate.name, space_name, ".desktop")
            == SHORTCUT_VERSION
            and candidate.name not in expected_desktops
        ):
            _remove_path(candidate)
    for candidate in icons.iterdir():
        if (
            _shortcut_version(candidate.name, space_name, ".png") == SHORTCUT_VERSION
            and candidate.name not in expected_icons
        ):
            _remove_path(candidate)


class InotifyMonitor:
    """Recursively monitor both guest application directories."""

    def __init__(self, rootfs: Path) -> None:
        self._fd = -1
        self._read_fd = -1
        self._write_fd = -1
        self._library = ctypes.CDLL(None, use_errno=True)
        self._library.inotify_init1.argtypes = [ctypes.c_int]
        self._library.inotify_init1.restype = ctypes.c_int
        self._library.inotify_add_watch.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        self._library.inotify_add_watch.restype = ctypes.c_int
        try:
            self._rootfs = rootfs.resolve(strict=True)
            self._fd = self._library.inotify_init1(os.O_CLOEXEC | os.O_NONBLOCK)
            if self._fd < 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
            self._read_fd, self._write_fd = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
            self._poll = select.poll()
            self._poll.register(self._fd, select.POLLIN)
            self._poll.register(self._read_fd, select.POLLIN)
            self._watches: set[int] = set()
            self.refresh()
        except Exception:
            self.close()
            raise

    def _add(self, path: Path) -> None:
        descriptor = self._library.inotify_add_watch(
            self._fd,
            os.fsencode(path),
            WATCH_MASK,
        )
        if descriptor >= 0:
            self._watches.add(descriptor)

    def _watch_root(self, guest_path: PurePosixPath) -> None:
        candidate = self._rootfs.joinpath(*guest_path.parts[1:])
        existing = candidate
        while not existing.exists() and existing != self._rootfs:
            existing = existing.parent
        try:
            fixed = existing.resolve(strict=True)
        except OSError:
            return
        if not fixed.is_relative_to(self._rootfs) or not fixed.is_dir():
            return
        self._add(fixed)
        if candidate.exists():
            for parent, directories, _files in os.walk(candidate, followlinks=False):
                parent_path = Path(parent)
                directories[:] = [
                    name
                    for name in directories
                    if not (parent_path / name).is_symlink()
                ]
                self._add(parent_path)

    def refresh(self) -> None:
        for _origin, guest_path, _prefix in SOURCE_DIRECTORIES:
            self._watch_root(guest_path)

    def wait(self, timeout: int | None = None) -> bool:
        events = self._poll.poll(timeout)
        if not events:
            return False
        changed = False
        for descriptor, flags in events:
            if flags & (select.POLLERR | select.POLLHUP | select.POLLNVAL):
                raise OSError("Shortcut inotify monitor failed.")
            if descriptor == self._read_fd:
                return False
            if descriptor != self._fd:
                continue
            while True:
                try:
                    payload = os.read(self._fd, 64 * 1024)
                except BlockingIOError:
                    break
                if not payload:
                    break
                offset = 0
                while offset + INOTIFY_EVENT.size <= len(payload):
                    _watch, mask, _cookie, length = INOTIFY_EVENT.unpack_from(
                        payload, offset
                    )
                    offset += INOTIFY_EVENT.size + length
                    if mask & (WATCH_MASK | IN_Q_OVERFLOW | IN_IGNORED):
                        changed = True
        return changed

    def stop(self) -> None:
        try:
            os.write(self._write_fd, b"\0")
        except (BlockingIOError, OSError):
            pass

    def close(self) -> None:
        for descriptor in (self._read_fd, self._write_fd, self._fd):
            if descriptor >= 0:
                os.close(descriptor)
        self._read_fd = -1
        self._write_fd = -1
        self._fd = -1


class ShortcutWorker:
    """Reconcile shortcut exports after guest application changes."""

    def __init__(
        self,
        space_name: str,
        rootfs: Path,
        distribution_id: str,
        monitor: InotifyMonitor,
        *,
        applications_root: Path | None = None,
    ) -> None:
        self._space_name = space_name
        self._rootfs = rootfs
        self._distribution_id = distribution_id
        self._monitor = monitor
        self._applications_root = applications_root or APPLICATIONS_ROOT
        self._stopping = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"spaces-{space_name}-shortcuts",
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        self._monitor.stop()

    def join(self) -> None:
        self._thread.join()

    def _run(self) -> None:
        try:
            while not self._stopping.is_set() and self._monitor.wait():
                if self._stopping.wait(0.15):
                    break
                while self._monitor.wait(0):
                    pass
                if self._stopping.is_set():
                    break
                reconcile(
                    self._space_name,
                    self._rootfs,
                    self._distribution_id,
                    applications_root=self._applications_root,
                )
                self._monitor.refresh()
        except Exception as error:
            logger.error(_("Application shortcut monitor failed: {error}", error=error))
