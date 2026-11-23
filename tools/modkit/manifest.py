"""GPL-3.0-or-later. Read the identity and display fields of a .psxmod manifest.

Only the framework's first, cheapest rules are mirrored here so a package can
be listed and refused early without a game build: TOML syntax, format_version
range, package id, semantic version and name. They follow the pinned
framework's mod_packages.cpp (valid_id, parse_semver, kMin/kMaxFormatVersion).
Everything else in the schema is checked by the runtime's own parser; see
validator.framework_errors.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import re

from . import ModError

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 source checks use the tomli backport.
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None

MIN_FORMAT = 1
MAX_FORMAT = 11  # Pinned framework f42d2c0: kMaxFormatVersion.
MAX_ID = 96
_ID = re.compile(r"[a-z0-9._-]+")
_SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.+-]+))?")


def valid_id(value) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= MAX_ID and _ID.fullmatch(value) is not None
            and not value.startswith(".") and not value.endswith("."))


def valid_version(value) -> bool:
    # The framework accepts any text after '-'. A version is also a directory
    # name here, so its suffix is restricted to portable path characters.
    return isinstance(value, str) and _SEMVER.fullmatch(value) is not None


@dataclass(frozen=True)
class Manifest:
    format_version: int
    id: str
    version: str
    name: str
    author: str = ""
    description: str = ""
    license: str = ""
    targets: tuple = ()
    features: tuple = ()
    plugins: tuple = ()
    resources: tuple = ()
    channel: str = "stable"
    extra: dict = field(default_factory=dict, compare=False, repr=False)

    def summary(self) -> dict:
        return dict(format_version=self.format_version, id=self.id, version=self.version, name=self.name,
                    author=self.author, description=self.description, license=self.license,
                    targets=list(self.targets), features=list(self.features), plugins=list(self.plugins),
                    resources=list(self.resources))

    def targets_game(self, game_id: str) -> bool:
        """A package without [[target]] makes no game claim; '*' is generic."""
        return not self.targets or "*" in self.targets or game_id in self.targets


def _text(value, name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ModError(f"Manifest field '{name}' must be text")
    return value


def parse(text: str) -> Manifest:
    if tomllib is None:
        raise ModError("This Python has no TOML reader; use Python 3.11 or newer")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ModError(f"manifest.toml is not valid TOML: {e}") from None
    fmt = data.get("format_version")
    if type(fmt) is not int or not MIN_FORMAT <= fmt <= MAX_FORMAT:
        raise ModError(f"Unsupported manifest format_version {fmt!r}; this build reads {MIN_FORMAT} to {MAX_FORMAT}")
    if not valid_id(data.get("id")):
        raise ModError("Invalid package id: use 1-96 lowercase letters, digits, '.', '_' or '-'")
    if not valid_version(data.get("version")):
        raise ModError("Invalid package version: use a semantic version such as 1.0.0")
    name = _text(data.get("name"), "name").strip()
    if not name:
        raise ModError("Package name is empty")

    def table_list(key):
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
            raise ModError(f"Manifest [[{key}]] entries are malformed")
        return value

    targets = tuple(_text(t.get("game_id"), "target.game_id") for t in table_list("target"))
    features = tuple(dict(id=_text(f.get("id"), "feature.id"), name=_text(f.get("name"), "feature.name"),
                          default_enabled=bool(f.get("default_enabled", False)))
                     for f in table_list("feature"))
    if any(not valid_id(f["id"]) for f in features) or len({f["id"] for f in features}) != len(features):
        raise ModError("Feature ids must be valid and unique")
    plugins = tuple(dict(id=_text(p.get("id"), "plugin.id"), feature=_text(p.get("feature"), "plugin.feature"))
                    for p in table_list("plugin"))
    if any(not valid_id(p["id"]) for p in plugins):
        raise ModError("Plugin ids must be valid")
    resources = []
    for r in table_list("resource"):
        size, sha = r.get("size"), r.get("sha256")
        verified = size is not None or sha is not None
        if verified and (type(size) is not int or size <= 0 or not isinstance(sha, str)
                         or re.fullmatch(r"[0-9a-f]{64}", sha) is None):
            raise ModError("Verified resources need a positive size and a lowercase SHA-256")
        resources.append(dict(id=_text(r.get("id"), "resource.id"), feature=_text(r.get("feature"), "resource.feature"),
                              format=_text(r.get("format"), "resource.format"), required=bool(r.get("required", False)),
                              size=size if verified else None, sha256=sha if verified else None))
    if any(not valid_id(r["id"]) for r in resources):
        raise ModError("Resource ids must be valid")
    return Manifest(format_version=fmt, id=data["id"], version=data["version"], name=name,
                    author=_text(data.get("author"), "author"), description=_text(data.get("description"), "description"),
                    license=_text(data.get("license"), "license"), targets=targets, features=features,
                    plugins=plugins, resources=tuple(resources),
                    channel=_text(data.get("channel"), "channel") or "stable", extra=data)


def read(path: Path) -> Manifest:
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise ModError(f"Cannot read {path.name}: {e.strerror or e}") from None
    try:
        return parse(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise ModError("manifest.toml must be UTF-8 text") from None
