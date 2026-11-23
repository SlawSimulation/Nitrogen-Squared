"""GPL-3.0-or-later. Durable .psxmod store and runtime staging.

Store layout (all under one root, normally .local/mods):

  packages/<id>/<version>/    extracted package contents; never edited after install
  metadata/<id>/<version>.json archive hash/size, install time, manifest summary
  selections.json             which installed packages the player enabled
  runtime-plugins.json        plugin ids reported by the last checked game build
  staging/                    scratch used while replacing a runtime's mods tree
  tmp/                        scratch used while installing an archive

Nothing here lives in the compiled game's directory, so rebuilding or
replacing .local/runtime-dev cannot delete installed packages. Before each
launch stage() rebuilds <exe>/mods/installed from the enabled packages and
writes the framework's state.toml (format 2). The framework-owned
<exe>/mods/bundled tree is never touched. With no enabled package and no
earlier staging, stage() changes nothing, so a mod-free launch is unchanged.
"""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid

from . import GAME_ID, ModError
from . import manifest as manifests
from . import validator

SCHEMA = 1
RECEIPT = "nitrogen-mods.json"
STATE = "state.toml"


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path, default):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        raise ModError(f"{path.name} is unreadable: {e}") from None
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ModError(f"{path.name} has an unsupported format")
    return value


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _remove_tree(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def runtime_build(work: Path):
    """(executable, executable_sha256) of a completed build in work, or None."""
    try:
        record = json.loads((work / "runtime-build.json").read_text(encoding="utf-8"))
        executable = Path(record["executable"])
        if record.get("build_succeeded") is not True or not executable.is_file():
            return None
        digest = (record.get("build_identity") or {}).get("executable_sha256") or _sha256(executable)
        return executable, digest
    except (OSError, ValueError, KeyError, TypeError):
        return None


def state_text(enabled: list[dict], resource_paths: dict | None = None) -> str:
    """The framework's format-2 selection file for these package records.

    resource_paths maps (package id, feature id) to {resource id: absolute path}
    for verified resources the package ships (see bind_resources)."""
    q = json.dumps
    lines = ["format_version = 2", ""]
    for record in enabled:
        lines += ["[[package]]", f"id = {q(record['id'])}", f"version = {q(record['version'])}", ""]
    for record in enabled:
        for feature in record["features"]:
            lines += ["[[feature]]", f"package_id = {q(record['id'])}", f"id = {q(feature['id'])}",
                      "enabled = true", ""]
            paths = (resource_paths or {}).get((record["id"], feature["id"]))
            if paths:
                lines += ["[feature.resources]"] + [f"{q(k)} = {q(v)}" for k, v in sorted(paths.items())] + [""]
    return "\n".join(lines)


def bind_resources(package_dir: Path, resources: list[dict]) -> dict:
    """Shipped files for verified resources: resources/<id>.<extension>, exactly
    matching the declared size and SHA-256. Returns {resource id: relative path}.

    The framework only reads verified resource bytes from a player-selected path
    (it forbids a package default for them). The launcher selects the package's
    own shipped copy on the player's behalf when it stages the package."""
    bound = {}
    for resource in resources:
        if not resource.get("sha256"):
            continue
        matches = sorted(p for p in (package_dir / "resources").glob(resource["id"] + ".*")
                         if p.is_file() and p.stem == resource["id"])
        if not matches:
            continue
        if len(matches) > 1:
            raise ModError(f"Package ships more than one file for resource {resource['id']}")
        path = matches[0]
        if path.stat().st_size != resource["size"] or _sha256(path) != resource["sha256"]:
            raise ModError(f"Shipped file for resource {resource['id']} does not match its declared size and SHA-256")
        bound[resource["id"]] = path.relative_to(package_dir).as_posix()
    return bound


class ModStore:
    def __init__(self, root: Path, game_id: str = GAME_ID):
        self.root = Path(root)
        self.game_id = game_id
        self.packages_dir = self.root / "packages"
        self.metadata_dir = self.root / "metadata"
        self.selections_path = self.root / "selections.json"
        self.plugins_path = self.root / "runtime-plugins.json"
        self.staging_dir = self.root / "staging"
        self.tmp_dir = self.root / "tmp"

    # ---- records -------------------------------------------------------
    def records(self) -> list[dict]:
        found = []
        if self.metadata_dir.is_dir():
            for path in sorted(self.metadata_dir.glob("*/*.json")):
                record = _read_json(path, None)
                if record and (self.packages_dir / record["id"] / record["version"]).is_dir():
                    found.append(record)
        return found

    def record(self, package_id: str) -> dict:
        for record in self.records():
            if record["id"] == package_id:
                return record
        raise ModError(f"Package {package_id} is not installed")

    def enabled_ids(self) -> set[str]:
        value = _read_json(self.selections_path, dict(schema=SCHEMA, enabled=[]))
        enabled = value.get("enabled")
        if not isinstance(enabled, list) or not all(isinstance(i, str) for i in enabled):
            raise ModError("selections.json has an unsupported format")
        return set(enabled)

    def _save_enabled(self, ids: set[str]) -> None:
        _write_json(self.selections_path, dict(schema=SCHEMA, enabled=sorted(ids)))

    # ---- build-backed checks ------------------------------------------
    def plugins(self, build) -> list[str] | None:
        if not build:
            return None
        executable, digest = build
        cached = _read_json(self.plugins_path, None) if self.plugins_path.exists() else None
        if cached and cached.get("executable_sha256") == digest:
            return cached["plugins"]
        plugins = validator.registered_plugins(executable)
        _write_json(self.plugins_path, dict(schema=SCHEMA, executable_sha256=digest, plugins=plugins))
        return plugins

    def status(self, build=None) -> list[dict]:
        """Every installed package with its enabled flag and compatibility."""
        records, enabled = self.records(), self.enabled_ids()
        plugins = self.plugins(build)
        errors = {}
        if build and records:
            roots = {r["id"]: (self.packages_dir / r["id"] / r["version"]).resolve() for r in records}
            for message in validator.framework_errors(build[0], list(roots.values())):
                for package_id, root in roots.items():
                    if message.startswith(str(root)):
                        errors.setdefault(package_id, validator.reason(message))
        result = []
        for record in records:
            required = sorted({p["id"] for p in record["plugins"]})
            missing = [] if plugins is None else [p for p in required if p not in plugins]
            unbound = [r["id"] for r in record.get("resources", []) if r.get("required")
                       and r["id"] not in record.get("bound_resources", {})]
            if not (not record["targets"] or "*" in record["targets"] or self.game_id in record["targets"]):
                state, detail = "other-game", "Made for " + ", ".join(record["targets"])
            elif unbound:
                state, detail = "needs-files", ("Needs files chosen in a resource picker, which this launcher "
                                                "does not offer yet: " + ", ".join(unbound))
            elif record["id"] in errors:
                state, detail = "invalid", "The game's package reader refused it: " + errors[record["id"]]
            elif plugins is None:
                state, detail = "unchecked", "Prepare the game once to check plugins and the full manifest"
            elif missing:
                state, detail = "missing-plugins", "This game build lacks " + ", ".join(missing)
            else:
                state, detail = "ready", "Compatible with the current game build"
            result.append(dict(record, required_plugins=required, missing_plugins=missing,
                               enabled=record["id"] in enabled,
                               compatibility=dict(state=state, detail=detail)))
        return result

    # ---- install / remove / select ------------------------------------
    def install(self, archive: Path, build=None) -> dict:
        archive = Path(archive)
        work = self.tmp_dir / ("install-" + uuid.uuid4().hex)
        try:
            validator.extract(archive, work / "package")
            info = manifests.read(work / "package" / "manifest.toml")
            existing = self.packages_dir / info.id
            if existing.exists():
                versions = sorted(p.name for p in existing.iterdir()) if existing.is_dir() else []
                if info.version in versions:
                    raise ModError(f"{info.name} {info.version} is already installed")
                raise ModError(f"Package id {info.id} is already installed (version {', '.join(versions) or 'unknown'}). "
                               f"Remove it before installing version {info.version}.")
            bound = bind_resources(work / "package", list(info.resources))
            validation = "basic"
            if build:
                errors = validator.framework_errors(build[0], [(work / "package").resolve()])
                if errors:
                    raise ModError("The game's package reader refused this manifest: " + validator.reason(errors[0]))
                validation = "framework"
            files = sum(1 for p in (work / "package").rglob("*") if p.is_file())
            record = dict(info.summary(), schema=SCHEMA, validation=validation, files=files, bound_resources=bound,
                          archive=dict(name=archive.name, sha256=_sha256(archive), size=archive.stat().st_size),
                          installed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            destination = self.packages_dir / info.id / info.version
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(work / "package", destination)
            try:
                _write_json(self.metadata_dir / info.id / f"{info.version}.json", record)
            except OSError:
                _remove_tree(destination)
                raise
            return record
        finally:
            _remove_tree(work)

    def remove(self, package_id: str) -> dict:
        record = self.record(package_id)
        enabled = self.enabled_ids()
        if package_id in enabled:
            self._save_enabled(enabled - {package_id})
        (self.metadata_dir / record["id"] / f"{record['version']}.json").unlink()
        trash = self.tmp_dir / ("remove-" + uuid.uuid4().hex)
        trash.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self.packages_dir / record["id"], trash)
        _remove_tree(trash)
        for parent in (self.metadata_dir / record["id"],):
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        return record

    def set_enabled(self, package_id: str, enabled: bool, build=None) -> dict:
        record = self.record(package_id)
        if enabled:
            current = next(r for r in self.status(build) if r["id"] == package_id)
            if current["compatibility"]["state"] not in ("ready", "unchecked"):
                raise ModError(f"{record['name']} cannot be enabled: {current['compatibility']['detail']}")
        ids = self.enabled_ids()
        self._save_enabled(ids | {package_id} if enabled else ids - {package_id})
        return record

    # ---- runtime staging ----------------------------------------------
    def stage(self, executable_dir: Path, build=None) -> dict:
        """Make <exe>/mods match the enabled packages. Refuses incompatible plans."""
        mods = Path(executable_dir) / "mods"
        receipt_path = mods / RECEIPT
        receipt = _read_json(receipt_path, None) if receipt_path.exists() else None
        # Only an enabled package needs the build checked; a mod-free launch runs nothing extra.
        enabled = self.enabled_ids() & {r["id"] for r in self.records()}
        if not enabled and receipt is None:
            return dict(changed=False, packages=[])
        selected = [r for r in self.status(build) if r["enabled"]] if enabled else []
        for record in selected:
            if record["compatibility"]["state"] != "ready":
                detail = ("Prepare the game before launching with mods" if record["compatibility"]["state"] == "unchecked"
                          else record["compatibility"]["detail"])
                raise ModError(f"Cannot launch with {record['name']}: {detail}. Disable it in Mods to play without it.")
        installed = mods / "installed"
        owned = {f"{p['id']}/{p['version']}" for p in (receipt or {}).get("packages", [])}
        if installed.is_dir():
            foreign = sorted(f"{d.name}/{v.name}" for d in installed.iterdir() if d.is_dir()
                             for v in d.iterdir() if f"{d.name}/{v.name}" not in owned)
            foreign += sorted(p.name for p in installed.iterdir() if not p.is_dir())
            if foreign:
                raise ModError(f"{installed} contains packages the launcher did not install ({', '.join(foreign)}). "
                               "Move them into a .psxmod and install it, or remove them, then try again.")
        run = self.staging_dir / ("run-" + uuid.uuid4().hex)
        try:
            fresh = run / "installed"
            fresh.mkdir(parents=True)
            for record in selected:
                shutil.copytree(self.packages_dir / record["id"] / record["version"],
                                fresh / record["id"] / record["version"])
            mods.mkdir(parents=True, exist_ok=True)
            if installed.exists():
                shutil.move(str(installed), str(run / "previous"))
            shutil.move(str(fresh), str(installed))
            state = mods / STATE
            backup = (receipt or {}).get("backup")
            paths = {}
            for record in selected:
                root = (installed / record["id"] / record["version"]).resolve()
                bound = record.get("bound_resources", {})
                for resource in record.get("resources", []):
                    if resource["id"] in bound:
                        paths.setdefault((record["id"], resource["feature"]), {})[resource["id"]] = \
                            str(root / bound[resource["id"]])
            if selected:
                if receipt is None and state.exists():
                    backup = f"{STATE}.before-nitrogen-mods-{datetime.now().strftime('%Y%m%d%H%M%S')}"
                    os.replace(state, mods / backup)
                temporary = mods / (STATE + ".tmp")
                temporary.write_text(state_text(selected, paths), encoding="utf-8", newline="\n")
                os.replace(temporary, state)
                _write_json(receipt_path, dict(schema=SCHEMA, backup=backup,
                                               packages=[dict(id=r["id"], version=r["version"]) for r in selected]))
            else:
                # Nothing is enabled any more: return to the condition before staging.
                if state.exists():
                    state.unlink()
                if backup and (mods / backup).exists():
                    os.replace(mods / backup, state)
                receipt_path.unlink()
        finally:
            _remove_tree(run)
        return dict(changed=True, packages=[dict(id=r["id"], version=r["version"]) for r in selected])
