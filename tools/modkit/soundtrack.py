"""GPL-3.0-or-later. Soundtrack project: ten CD-DA slots, replacements and export.

N2O plays disc audio tracks 2-11. A project assigns each of those ten slots
either an original disc track (unchanged, or moved from another slot) or a
replacement converted from the player's own WAVE file. Exports are .psxmod
packages for the trusted "nitrogen.soundtrack" plugin and contain only the
replacement audio, a playlist and the manifest; original game audio is always
read from the player's mounted disc at runtime and is never copied.

Storage (normally .local/soundtrack):
  project.json                 slot assignments and package details
  replacements/<sha256>.pcm    converted raw CD-DA sectors (content addressed)
  previews/                    temporary preview WAVE files
  exports/<id>-<version>.psxmod
"""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import zipfile

from n2o_disc import DiscImage  # tools/ is on sys.path whenever modkit is importable

from . import GAME_ID, ModError
from . import audio, manifest as manifests, validator

SCHEMA = 1
PLUGIN = "nitrogen.soundtrack"
FEATURE = "soundtrack"
SLOTS = tuple(range(2, 12))          # N2O disc audio tracks 2-11; the game expects exactly ten
PLAYLIST_HEADER = "nitrogen-soundtrack 1"
PREVIEW_SECONDS = 30
PLAYLIST_PREVIEW_SECONDS = 8


def original_tracks(installation: Path) -> dict:
    """{track number: (disc files, first sector, sector count)} from the verified import.

    A track runs from INDEX 01 to the next track's INDEX 00 (or the disc end),
    which is exactly the slice the framework mounts for a disc track. Sectors
    are absolute disc LBAs; `disc files` maps them to the BIN(s) holding them,
    so single- and multi-BIN imports are read the same way."""
    try:
        record = json.loads((Path(installation) / "installation.json").read_text(encoding="utf-8"))
        with DiscImage.from_record(record) as disc:
            image, total = tuple(disc.files), disc.sectors
        tracks = record["tracks"]
    except (OSError, ValueError, KeyError, TypeError):
        raise ModError("Choose and verify your game disc first") from None
    result = {}
    for i, track in enumerate(tracks):
        if track.get("kind") != "AUDIO":
            continue
        following = tracks[i + 1] if i + 1 < len(tracks) else None
        end = (following.get("pregap") or following["start"]) if following else total
        result[track["number"]] = (image, track["start"], end - track["start"])
    if sorted(result) != list(SLOTS):
        raise ModError("The imported disc does not have N2O's ten audio tracks")
    return result


def default_details() -> dict:
    return dict(id="custom.soundtrack", name="My N2O Soundtrack", version="1.0.0", author="", license="",
                description="Replacement soundtrack for N2O.")


class SoundtrackProject:
    def __init__(self, root: Path, installation: Path, game_id: str = GAME_ID):
        self.root = Path(root)
        self.game_id = game_id
        self.installation = Path(installation)
        self.path = self.root / "project.json"
        self.replacements = self.root / "replacements"
        self.previews = self.root / "previews"
        self.exports = self.root / "exports"

    # ---- state ---------------------------------------------------------
    def load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return dict(schema=SCHEMA, slots={str(n): dict(kind="disc", track=n) for n in SLOTS},
                        details=default_details())
        except (OSError, ValueError) as e:
            raise ModError(f"Soundtrack project is unreadable: {e}") from None
        if value.get("schema") != SCHEMA or sorted(map(int, value.get("slots", {}))) != list(SLOTS):
            raise ModError("Soundtrack project has an unsupported format")
        value.setdefault("details", default_details())
        return value

    def save(self, value: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name("project.json.tmp")
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)
        self._collect(value)

    def _collect(self, value: dict) -> None:
        """Delete converted replacements no slot uses any more."""
        used = {s["file"] for s in value["slots"].values() if s["kind"] == "replacement"}
        if self.replacements.is_dir():
            for path in self.replacements.glob("*.pcm"):
                if path.name not in used:
                    path.unlink()

    def status(self) -> dict:
        value, originals = self.load(), original_tracks(self.installation)
        rows = []
        for slot in SLOTS:
            source = value["slots"][str(slot)]
            if source["kind"] == "disc":
                sectors = originals[source["track"]][2]
                state = "original" if source["track"] == slot else "moved"
                label = f"Original track {source['track']:02d}"
            else:
                sectors = source["sectors"]
                state = "replacement"
                label = source["name"]
            rows.append(dict(slot=slot, kind=source["kind"], track=source.get("track"), label=label, state=state,
                             sectors=sectors, seconds=round(audio.sectors_seconds(sectors), 2),
                             format=audio.FORMAT_LABEL, size=sectors * audio.SECTOR))
        replacement_bytes = sum({s["file"]: s["sectors"] * audio.SECTOR
                                 for s in value["slots"].values() if s["kind"] == "replacement"}.values())
        changed = any(r["state"] != "original" for r in rows)
        return dict(slots=rows, details=value["details"], changed=changed, replacement_bytes=replacement_bytes,
                    limit_bytes=audio.MAX_PACKAGE_BYTES - 1024 * 1024)

    # ---- editing -------------------------------------------------------
    @staticmethod
    def _slot(slot) -> str:
        if type(slot) is not int or slot not in SLOTS:
            raise ModError("Choose a soundtrack slot from 2 to 11")
        return str(slot)

    def import_wave(self, slot: int, wave_path: Path) -> dict:
        key = self._slot(slot)
        info = audio.read_wave(Path(wave_path))
        self.replacements.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.replacements) as scratch:
            converted = Path(scratch) / "converted.pcm"
            result = audio.convert(info, converted)
            target = self.replacements / f"{result['sha256']}.pcm"
            if not target.exists():
                os.replace(converted, target)
        value = self.load()
        value["slots"][key] = dict(kind="replacement", file=target.name, name=Path(wave_path).name,
                                   sectors=result["sectors"], sha256=result["sha256"],
                                   imported_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self._check_budget(value)
        self.save(value)
        return value["slots"][key]

    def restore(self, slot: int) -> None:
        value = self.load()
        value["slots"][self._slot(slot)] = dict(kind="disc", track=slot)
        self.save(value)

    def reset(self) -> None:
        value = self.load()
        value["slots"] = {str(n): dict(kind="disc", track=n) for n in SLOTS}
        self.save(value)

    def move(self, slot: int, offset: int) -> None:
        """Swap a slot's source with its neighbour (offset -1 or +1)."""
        key = self._slot(slot)
        if offset not in (-1, 1) or slot + offset not in SLOTS:
            raise ModError("That slot cannot move further")
        value = self.load()
        other = str(slot + offset)
        value["slots"][key], value["slots"][other] = value["slots"][other], value["slots"][key]
        self.save(value)

    def set_details(self, details: dict) -> dict:
        value = self.load()
        merged = dict(value["details"])
        for key in ("id", "name", "version", "author", "license", "description"):
            if key in details:
                if not isinstance(details[key], str) or len(details[key]) > 300:
                    raise ModError(f"Package {key} must be text")
                merged[key] = details[key].strip()
        if not manifests.valid_id(merged["id"]):
            raise ModError("Package id: use lowercase letters, digits, '.', '_' or '-' (for example yourname.soundtrack)")
        if not manifests.valid_version(merged["version"]):
            raise ModError("Version: use a semantic version such as 1.0.0")
        if not merged["name"]:
            raise ModError("Give the package a name")
        value["details"] = merged
        self.save(value)
        return merged

    def _check_budget(self, value: dict) -> None:
        total = sum({s["file"]: s["sectors"] * audio.SECTOR
                     for s in value["slots"].values() if s["kind"] == "replacement"}.values())
        limit = audio.MAX_PACKAGE_BYTES - 1024 * 1024
        if total > limit:
            raise ModError(f"Replacement audio would total {total / 2**20:.0f} MiB; one package holds at most "
                           f"{limit / 2**20:.0f} MiB (about {audio.sectors_seconds(limit // audio.SECTOR) / 60:.0f} minutes)")

    # ---- previews ------------------------------------------------------
    def _source_chunks(self, source: dict, originals: dict, start: int, length: int):
        """Yield raw CD-DA bytes [start, start+length) of one slot source."""
        if source["kind"] == "disc":
            files, first, sectors = originals[source["track"]]
            length = max(0, min(length, sectors * audio.SECTOR - start))
            with DiscImage(files) as disc:
                yield from disc.read_bytes(first * audio.SECTOR + start, length)
            return
        path, base, size = self.replacements / source["file"], 0, source["sectors"] * audio.SECTOR
        length = max(0, min(length, size - start))
        with open(path, "rb") as handle:
            handle.seek(base + start)
            while length:
                chunk = handle.read(min(length, 1024 * 1024))
                if not chunk:
                    raise ModError("Audio source ended early")
                length -= len(chunk)
                yield chunk

    def _clear_previews(self) -> None:
        if self.previews.is_dir():
            for old in self.previews.glob("*.wav"):
                try:
                    old.unlink()
                except OSError:
                    pass    # a preview still playing on Windows stays until the next one

    def preview(self, slot: int | None = None) -> Path:
        """A WAVE of one slot's first seconds, or of every slot in playlist order."""
        value, originals = self.load(), original_tracks(self.installation)
        self.previews.mkdir(parents=True, exist_ok=True)
        self._clear_previews()
        if slot is None:
            parts = [(value["slots"][str(n)], PLAYLIST_PREVIEW_SECONDS) for n in SLOTS]
            name = "playlist"
        else:
            parts = [(value["slots"][self._slot(slot)], PREVIEW_SECONDS)]
            name = f"slot-{slot:02d}"
        pieces, total = [], 0
        for source, seconds in parts:
            size = (originals[source["track"]][2] if source["kind"] == "disc" else source["sectors"]) * audio.SECTOR
            length = min(size, seconds * audio.BYTES_PER_SECOND)
            pieces.append((source, length))
            total += length
        target = self.previews / f"{name}-{datetime.now().strftime('%H%M%S%f')}.wav"

        def chunks():
            for source, length in pieces:
                yield from self._source_chunks(source, originals, 0, length)
        audio.write_wave(target, chunks(), total)
        return target

    # ---- export ---------------------------------------------------------
    def build_package(self, destination: Path) -> dict:
        """Write the .psxmod: manifest, playlist and only the replacement audio."""
        value = self.load()
        details = value["details"]
        if not any(s != dict(kind="disc", track=int(k)) for k, s in value["slots"].items()):
            raise ModError("Replace or move at least one track before exporting")
        self._check_budget(value)
        files, resources, lines = {}, [], [PLAYLIST_HEADER]
        audio_ids = {}
        for slot in SLOTS:
            source = value["slots"][str(slot)]
            if source["kind"] == "disc":
                lines.append(f"{slot} disc {source['track']}")
                continue
            if source["file"] not in audio_ids:
                resource_id = f"audio-{len(audio_ids) + 1}"
                audio_ids[source["file"]] = resource_id
                path = self.replacements / source["file"]
                if not path.is_file() or path.stat().st_size != source["sectors"] * audio.SECTOR:
                    raise ModError(f"Converted audio for slot {slot} is missing; import it again")
                files[f"resources/{resource_id}.pcm"] = path
                resources.append((resource_id, f"Replacement audio {len(audio_ids)}", "*.pcm",
                                  path.stat().st_size, source["sha256"]))
            lines.append(f"{slot} resource {audio_ids[source['file']]}")
        playlist = ("\n".join(lines) + "\n").encode("ascii")
        resources.insert(0, ("playlist", "Soundtrack playlist", "*.txt", len(playlist),
                             hashlib.sha256(playlist).hexdigest()))
        manifest = _manifest_text(details, resources, self.game_id)
        manifests.parse(manifest)  # Same early checks as installation.
        readme = (f"{details['name']} {details['version']}\n\nA soundtrack package for Nitrogen Squared (N2O, {GAME_ID}).\n"
                  "Install it from the launcher's Mods page. It contains only the author's replacement audio;\n"
                  "unchanged slots play the original tracks from your own disc.\n\nPlaylist:\n" + playlist.decode())
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        with zipfile.ZipFile(temporary, "w") as archive:
            def add(name, data=None, path=None, compress=zipfile.ZIP_DEFLATED):
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                info.compress_type = compress
                info.external_attr = 0o100644 << 16
                if path is None:
                    archive.writestr(info, data)
                else:
                    with archive.open(info, "w") as out, open(path, "rb") as source:
                        shutil.copyfileobj(source, out, 1024 * 1024)
            add("manifest.toml", manifest.encode("utf-8"))
            add("README.txt", readme.encode("utf-8"))
            add("resources/playlist.txt", playlist)
            for name, path in files.items():
                add(name, path=path, compress=zipfile.ZIP_STORED)   # PCM barely compresses; storing is faster
        os.replace(temporary, destination)
        return dict(path=str(destination), size=destination.stat().st_size, replacements=len(audio_ids),
                    playlist=lines[1:])

    def export(self, build=None) -> dict:
        """Build, then validate the archive exactly as installation would."""
        details = self.load()["details"]
        target = self.exports / f"{details['id']}-{details['version']}.psxmod"
        result = self.build_package(target)
        try:
            check_package(target, build)
        except ModError:
            target.unlink(missing_ok=True)
            raise
        return result


def _manifest_text(details: dict, resources: list, game_id: str = GAME_ID) -> str:
    q = json.dumps
    lines = ["format_version = 10", f"id = {q(details['id'])}", f"version = {q(details['version'])}",
             f"name = {q(details['name'])}"]
    for key in ("author", "description", "license"):
        if details.get(key):
            lines.append(f"{key} = {q(details[key])}")
    lines += ['resolver = "declarative"', 'save_compatibility = "shared"', "",
              "[[target]]", f"game_id = {q(game_id)}", "",
              "[[feature]]", f"id = {q(FEATURE)}", 'name = "Replacement soundtrack"',
              'description = "Plays this package\'s soundtrack playlist instead of the original CD audio order."',
              'group = "Audio"', "default_enabled = false", "",
              "[[plugin]]", f"feature = {q(FEATURE)}", f"id = {q(PLUGIN)}"]
    for resource_id, label, patterns, size, sha in resources:
        lines += ["", "[[resource]]", f"feature = {q(FEATURE)}", f"id = {q(resource_id)}", f"label = {q(label)}",
                  'format = "file"', f"file_patterns = {q(patterns)}", "required = true",
                  f"size = {size}", f"sha256 = {q(sha)}"]
    return "\n".join(lines) + "\n"


def check_package(path: Path, build=None) -> None:
    """Archive rules, manifest, playlist and shipped resources; the game build's parser when present."""
    from .package import bind_resources
    with tempfile.TemporaryDirectory(prefix="soundtrack-check-") as scratch:
        root = Path(scratch) / "package"
        validator.extract(Path(path), root)
        info = manifests.read(root / "manifest.toml")
        bound = bind_resources(root, list(info.resources))
        missing = [r["id"] for r in info.resources if r["required"] and r["id"] not in bound]
        if missing:
            raise ModError("Export is missing resource files: " + ", ".join(missing))
        playlist = (root / bound["playlist"]).read_text(encoding="ascii").splitlines()
        if playlist[0] != PLAYLIST_HEADER or len(playlist) != len(SLOTS) + 1:
            raise ModError("Export playlist is malformed")
        for slot, line in zip(SLOTS, playlist[1:]):
            number, kind, ref = line.split()
            if int(number) != slot or kind not in ("disc", "resource") or (kind == "resource" and ref not in bound):
                raise ModError(f"Export playlist slot {slot} is invalid")
            if kind == "resource" and (root / bound[ref]).stat().st_size % audio.SECTOR:
                raise ModError(f"Export audio {ref} is not whole CD-DA sectors")
        if build:
            errors = validator.framework_errors(build[0], [root.resolve()])
            if errors:
                raise ModError("The game's package reader refused the export: " + validator.reason(errors[0]))
