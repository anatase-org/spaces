"""Content-addressed native test builds, shared safely by parallel workers."""
from __future__ import annotations

import fcntl
import hashlib
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def compile_native(name, sources, flags=()):
    compiler = shlex.split(os.environ.get("CC", "cc"))
    sources = [str(source) for source in sources]
    flags = list(flags)
    digest = hashlib.sha256()
    digest.update(repr((compiler, sources, flags)).encode())
    # Include transitive project headers, including the vendored JSON parser.
    # Compiler identity invalidates the cache on toolchain upgrades as well.
    digest.update(subprocess.check_output([*compiler, "--version"]))
    for directory in (ROOT / "native", ROOT / "tests"):
        for path in sorted(directory.rglob("*")):
            if path.suffix in (".c", ".h"):
                digest.update(str(path.relative_to(ROOT)).encode())
                digest.update(path.read_bytes())
    for source in sources:
        digest.update(Path(source).read_bytes())
    directory = ROOT / ".build" / "tests" / digest.hexdigest()[:24]
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / name
    with (directory / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not output.exists():
            # Publish only successful, complete builds.
            with tempfile.TemporaryDirectory(dir=directory) as temporary:
                target = Path(temporary) / name
                try:
                    subprocess.run([*compiler, *sources, *flags, "-o", str(target)],
                                   check=True, capture_output=True, text=True)
                except subprocess.CalledProcessError as error:
                    raise RuntimeError(f"Native test build failed: {name}\n{error.stderr}") from error
                target.replace(output)
    return output
