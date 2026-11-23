"""GPL-3.0-or-later. Archive safety and framework-backed package validation.

Archive limits mirror the pinned framework's parse_zip/safe_archive_name so a
package refused by its installer is refused here too: at most 4096 entries and
256 MiB expanded, stored or DEFLATE entries only, no encryption, and no
absolute, drive-qualified, '.' or '..' paths. manifest.toml must be at the root.

The complete manifest schema and the set of linked trusted plugins are not
reimplemented. They come from the built runtime's --audit-mod-plugins mode,
which parses each manifest with ModPackageManager::read_manifest and lists the
plugin ids registered in that executable. It starts no emulation.
"""
from __future__ import annotations
from pathlib import Path, PurePosixPath
import os
import re
import shutil
import subprocess
import tempfile
import zipfile

from . import ModError

MAX_ENTRIES = 4096
MAX_BYTES = 256 * 1024 * 1024
MAX_NAME = 512
_UNDECLARED = re.compile(r"^\s*UNDECLARED \(can never run\): (\S+)\s*$")
_SUMMARY = re.compile(r"^mod plugin audit: (\d+) manifest\(s\), (\d+) declared id\(s\), (\d+) registered id\(s\)")
_ERROR = re.compile(r"^\s*manifest error: (.*)$")


def safe_name(name: str) -> bool:
    if not name or len(name) > MAX_NAME or name[0] in "/\\":
        return False
    if len(name) >= 2 and name[0].isalpha() and name[1] == ":":
        return False
    return all(part not in ("", ".", "..") for part in name.split("/"))


def inspect_archive(path: Path) -> list[zipfile.ZipInfo]:
    """Return the archive's file entries or refuse it, without extracting."""
    if path.suffix.lower() != ".psxmod":
        raise ModError("Choose a .psxmod package")
    try:
        if not path.is_file():
            raise ModError("The selected package file does not exist")
        if path.stat().st_size > MAX_BYTES:
            raise ModError("Package archive is larger than 256 MiB")
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise ModError("The package is not a ZIP-based .psxmod archive") from None
    except OSError as e:
        raise ModError(f"Cannot read the package: {e.strerror or e}") from None
    with archive:
        entries = archive.infolist()
        if len(entries) > MAX_ENTRIES:
            raise ModError("Package has more than 4096 entries")
        total, seen, files = 0, set(), []
        for info in entries:
            name = info.filename.replace("\\", "/")
            directory = name.endswith("/")
            if not safe_name(name[:-1] if directory else name):
                raise ModError(f"Unsafe path in package: {info.filename}")
            if info.flag_bits & 1:
                raise ModError("Encrypted package entries are not supported")
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise ModError("Package uses an unsupported compression method")
            total += info.file_size
            if total > MAX_BYTES:
                raise ModError("Package expands beyond 256 MiB")
            key = name.rstrip("/").casefold()  # Windows and macOS stores are case-insensitive.
            if key in seen:
                raise ModError(f"Package contains a duplicate path: {info.filename}")
            seen.add(key)
            if not directory:
                files.append(info)
        if not any(i.filename == "manifest.toml" for i in files):
            raise ModError("Package root does not contain manifest.toml (archive the folder's contents)")
        return files


def extract(path: Path, destination: Path) -> None:
    """Extract an inspected archive into a new directory, refusing escapes."""
    files = inspect_archive(path)
    destination.mkdir(parents=True)
    root = destination.resolve()
    with zipfile.ZipFile(path) as archive:
        for info in files:
            target = (root / PurePosixPath(info.filename.replace("\\", "/"))).resolve()
            if not target.is_relative_to(root) or target == root:
                raise ModError(f"Unsafe path in package: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, open(target, "xb") as out:
                shutil.copyfileobj(source, out, 1024 * 1024)


def _audit(executable: Path, roots: list[Path]) -> str:
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: never flash a console.
    try:
        result = subprocess.run([str(executable), "--audit-mod-plugins", *map(str, roots)],
                                cwd=executable.parent, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=60, creationflags=flags)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ModError(f"Could not run the game build's package check: {e}") from None
    if not any(_SUMMARY.match(line) for line in result.stdout.splitlines()):
        raise ModError("The game build did not report its package check; rebuild the game")
    return result.stdout


def registered_plugins(executable: Path) -> list[str]:
    """Plugin ids linked into this executable, as reported by the framework."""
    with tempfile.TemporaryDirectory(prefix="modkit-audit-") as empty:
        out = _audit(executable, [Path(empty)])
    plugins = sorted({m[1] for line in out.splitlines() if (m := _UNDECLARED.match(line))})
    counted = next(int(m[3]) for line in out.splitlines() if (m := _SUMMARY.match(line)))
    if counted != len(plugins):
        raise ModError("The game build's plugin report was incomplete")
    return plugins


def framework_errors(executable: Path, package_roots: list[Path]) -> list[str]:
    """Manifest errors from the framework parser; empty when every manifest loads.

    Each message is '<manifest path>: <reason>' (the parser may repeat the
    path inside its reason); reason() extracts the player-facing part."""
    if not package_roots:
        return []
    out = _audit(executable, package_roots)
    return [m[1] for line in out.splitlines() if (m := _ERROR.match(line))]


def reason(message: str) -> str:
    marker = "manifest.toml: "
    return message.rsplit(marker, 1)[-1] if marker in message else message
