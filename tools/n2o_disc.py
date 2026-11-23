#!/usr/bin/env python3
"""Bounded, read-only N2O disc inspection and local executable preparation.

Python 3.10+, standard library only. No game bytes are embedded in this module.
A CUE sheet may keep the disc in one BIN or split it across several BINs (for
example one file per track). Its FILE/TRACK/INDEX metadata is followed exactly:
the files are laid out on the disc in CUE order, INDEX times are relative to
their FILE, and the resulting absolute layout, size and SHA-256 of the disc's
sectors must equal the verified revision. Track files are only ever read.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

ROOT = Path(__file__).resolve().parents[1]
SYNC = b"\x00" + b"\xff" * 10 + b"\x00"
SECTOR = 2352
USER = 2048
MAX_EXE = 2 * 1024 * 1024


class DiscError(ValueError):
    pass


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class Track:
    number: int
    kind: str
    start: int
    pregap: int | None


def frames(s: str) -> int:
    m, sec, frame = map(int, s.split(":"))
    if sec >= 60 or frame >= 75:
        raise DiscError("Invalid CUE timestamp")
    return (m * 60 + sec) * 75 + frame


DATA_MODES = ("MODE1/2352", "MODE2/2352")
TRACK_MODES = DATA_MODES + ("AUDIO",)
# Descriptive CUE metadata that does not change where any sector is stored.
METADATA = ("REM", "CATALOG", "TITLE", "PERFORMER", "SONGWRITER", "ISRC", "FLAGS")


@dataclass(frozen=True)
class TrackFile:
    """One BIN named by a FILE line, placed at disc LBA `start`."""
    name: str
    path: Path
    start: int
    sectors: int


@dataclass(frozen=True)
class Disc:
    """A CUE sheet's complete layout. Track positions are absolute disc LBAs."""
    cue: Path
    files: tuple[TrackFile, ...]
    tracks: tuple[Track, ...]
    track_files: tuple[int, ...]   # index into files for each track

    @property
    def sectors(self) -> int:
        return sum(f.sectors for f in self.files)

    @property
    def data_file(self) -> Path:
        """The BIN holding Track 1, the data track (the whole image for a single-BIN CUE)."""
        return self.files[self.track_files[0]].path

    @property
    def data_end(self) -> int:
        """First LBA after the data track: Track 2's INDEX 00 (or 01), else the disc end."""
        if len(self.tracks) < 2:
            return self.sectors
        following = self.tracks[1]
        return following.pregap if following.pregap is not None else following.start


def _cue_file_name(rest: str) -> tuple[str, str]:
    m = re.fullmatch(r'"([^"]+)"\s+(\S+)', rest) or re.fullmatch(r"(\S+)\s+(\S+)", rest)
    if not m:
        raise DiscError(f"Malformed CUE FILE line: FILE {rest}")
    return m[1], m[2].upper()


def _resolve_track_file(cue: Path, name: str) -> Path:
    """A BIN path relative to the CUE's folder; it must stay inside that folder."""
    wp = PureWindowsPath(name)
    parts = re.split(r"[\\/]", name)
    if wp.is_absolute() or wp.drive or name.startswith(("/", "\\")) or any(p in ("", ".", "..") for p in parts):
        raise DiscError(f"The CUE must reference its BIN files by a path relative to the CUE folder: {name}")
    folder = cue.parent.resolve()
    path = folder.joinpath(*parts)
    if not path.exists() and path.parent.is_dir():
        # A CUE written on Windows may differ in letter case from the files on disk.
        matches = [p for p in path.parent.iterdir() if p.name.casefold() == path.name.casefold()]
        if len(matches) == 1:
            path = matches[0]
    path = path.resolve()
    if folder not in path.parents:
        raise DiscError(f"BIN file is outside the CUE folder: {name}")
    if not path.is_file():
        raise DiscError(f"BIN file referenced by the CUE is missing: {name}. Keep the CUE and all its BIN files together.")
    try:
        with open(path, "rb") as handle:
            handle.read(1)
    except OSError as e:
        raise DiscError(f"BIN file referenced by the CUE cannot be read: {name} ({e.strerror or e})") from None
    return path


def read_cue(cue: Path) -> Disc:
    """Parse and validate a single- or multi-BIN CUE sheet without reading sector contents.

    Each FILE occupies the next contiguous run of disc sectors. INDEX times are
    relative to the FILE they appear under, so a track's absolute LBA is its
    file's start plus its INDEX. Several tracks may share one BIN and a track
    may begin anywhere inside its file; a track may not continue into a later
    FILE, because the framework that mounts the disc does not support that."""
    cue = cue.resolve()
    try:
        text = cue.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise DiscError("The CUE sheet is not UTF-8 text") from None
    files: list[dict] = []
    tracks: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        keyword, rest = (re.split(r"\s+", line, maxsplit=1) + [""])[:2]
        keyword, rest = keyword.upper(), rest.strip()
        if keyword in METADATA:
            continue
        if keyword == "FILE":
            name, kind = _cue_file_name(rest)
            if kind != "BINARY":
                raise DiscError(f"BIN file {name} has type {kind}; only raw BINARY track files are supported")
            files.append(dict(name=name, tracks=[]))
            continue
        if keyword == "TRACK":
            m = re.fullmatch(r"(\d{1,2})\s+(\S+)", rest)
            if not m:
                raise DiscError(f"Malformed CUE TRACK line: {line}")
            if not files:
                raise DiscError("The CUE lists a TRACK before any FILE")
            mode = m[2].upper()
            if mode not in TRACK_MODES:
                raise DiscError(f"Track {int(m[1])} uses {mode}; only raw 2,352-byte sectors "
                                "(AUDIO, MODE1/2352 or MODE2/2352) are supported")
            track = dict(number=int(m[1]), kind=mode, file=len(files) - 1, indexes={})
            tracks.append(track)
            files[-1]["tracks"].append(track)
            continue
        if keyword == "INDEX":
            m = re.fullmatch(r"(\d{1,2})\s+(\d+:\d+:\d+)", rest)
            if not m:
                raise DiscError(f"Malformed CUE INDEX line: {line}")
            if not tracks:
                raise DiscError("The CUE lists an INDEX before any TRACK")
            track = tracks[-1]
            if track["file"] != len(files) - 1:
                raise DiscError(f"Track {track['number']} continues into another FILE; "
                                "a track's INDEX lines must follow its own FILE")
            number, position = int(m[1]), frames(m[2])
            if number in track["indexes"]:
                raise DiscError(f"Duplicate INDEX {number:02d} in track {track['number']}")
            if track["indexes"] and (number < max(track["indexes"]) or position < max(track["indexes"].values())):
                raise DiscError(f"INDEX lines of track {track['number']} are out of order")
            track["indexes"][number] = position
            continue
        if keyword in ("PREGAP", "POSTGAP"):
            raise DiscError(f"{keyword} adds sectors that are not stored in the BIN files; "
                            "use a dump that stores every sector (INDEX 00 pregaps)")
        raise DiscError(f"Unsupported CUE directive: {line}")
    if not files or not tracks:
        raise DiscError("The CUE sheet lists no FILE and TRACK entries")

    placed: list[TrackFile] = []
    seen: set[Path] = set()
    disc_lba = 0
    for entry in files:
        path = _resolve_track_file(cue, entry["name"])
        if path in seen:
            raise DiscError(f"BIN file {entry['name']} is listed more than once")
        seen.add(path)
        size = path.stat().st_size
        if not size or size % SECTOR:
            raise DiscError(f"BIN file {entry['name']} is not a whole number of raw 2,352-byte sectors")
        if not entry["tracks"]:
            raise DiscError(f"BIN file {entry['name']} has no TRACK")
        sectors = size // SECTOR
        previous_start = -1
        for track in entry["tracks"]:
            indexes = track["indexes"]
            if 1 not in indexes:
                raise DiscError(f"Track {track['number']} has no INDEX 01")
            region = indexes.get(0, indexes[1])
            if not previous_start < region <= indexes[1] < sectors:
                raise DiscError(f"Track {track['number']} lies outside or overlaps the sectors of {entry['name']}")
            previous_start = indexes[1]
        placed.append(TrackFile(entry["name"], path, disc_lba, sectors))
        disc_lba += sectors

    layout = []
    for i, track in enumerate(tracks, 1):
        if track["number"] != i:
            raise DiscError("Tracks must be numbered 01, 02, 03 … in order")
        base = placed[track["file"]].start
        gap = track["indexes"].get(0)
        layout.append(Track(track["number"], track["kind"], base + track["indexes"][1],
                            None if gap is None else base + gap))
    total, previous = disc_lba, -1
    for track in layout:
        if not previous < track.start < total:
            raise DiscError("Invalid or non-monotonic track layout")
        if track.pregap is not None and not previous < track.pregap <= track.start:
            raise DiscError("Invalid track pregap")
        previous = track.start
    if layout[0].start != 0 or layout[0].kind != "MODE2/2352":
        raise DiscError("Track 1 must start at sector zero in MODE2/2352")
    if any(t.kind != "AUDIO" for t in layout[1:]):
        raise DiscError("Expected audio tracks after Track 1 (one data track followed by CD audio)")
    disc = Disc(cue, tuple(placed), tuple(layout), tuple(t["file"] for t in tracks))
    data = placed[disc.track_files[0]]
    if not data.start <= disc.data_end <= data.start + data.sectors:
        raise DiscError("The data track must be stored completely in its own BIN file")
    return disc


def parse_cue(cue: Path) -> tuple[Path, list[Track]]:
    """Compatibility form of read_cue: (data-track BIN, tracks with absolute LBAs)."""
    disc = read_cue(cue)
    return disc.data_file, list(disc.tracks)


class DiscImage:
    """Read-only access to a disc's raw sectors by absolute LBA across its BIN files."""

    def __init__(self, files):
        self.files = [(Path(path), int(start), int(sectors)) for path, start, sectors in files]
        expected = 0
        for _, start, sectors in self.files:
            if start != expected or sectors <= 0:
                raise DiscError("Disc files must be contiguous")
            expected += sectors
        self.sectors = expected
        self._handles: dict[int, object] = {}

    @classmethod
    def from_disc(cls, disc: Disc) -> "DiscImage":
        return cls((f.path, f.start, f.sectors) for f in disc.files)

    @classmethod
    def from_record(cls, record: dict) -> "DiscImage":
        """From an inspect()/installation.json record; records before multi-BIN support name one BIN."""
        if record.get("files"):
            return cls((f["path"], f["start"], f["sectors"]) for f in record["files"])
        path = Path(record["bin"])
        return cls([(path, 0, path.stat().st_size // SECTOR)])

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        for handle in self._handles.values():
            handle.close()
        self._handles.clear()

    def read_bytes(self, offset: int, length: int, chunk: int = 1024 * 1024):
        """Yield disc bytes [offset, offset+length), crossing BIN boundaries as the CUE lays them out."""
        if offset < 0 or length < 0 or offset + length > self.sectors * SECTOR:
            raise DiscError("Read beyond the end of the disc")
        for i, (path, start, sectors) in enumerate(self.files):
            first, end = start * SECTOR, (start + sectors) * SECTOR
            while length and first <= offset < end:
                handle = self._handles.get(i)
                if handle is None:
                    handle = self._handles[i] = path.open("rb")
                handle.seek(offset - first)
                count = min(length, end - offset, chunk)
                data = handle.read(count)
                if len(data) != count:
                    raise DiscError(f"BIN file {path.name} changed or ended while reading")
                yield data
                offset += count
                length -= count
            if not length:
                return

    def read(self, lba: int, count: int = 1) -> bytes:
        return b"".join(self.read_bytes(lba * SECTOR, count * SECTOR))

    def chunks(self, lba: int, count: int, per: int = 256):
        """Raw sectors [lba, lba+count) in pieces of at most `per` sectors."""
        return self.read_bytes(lba * SECTOR, count * SECTOR, per * SECTOR)

    def sha256(self) -> str:
        """SHA-256 of every disc sector in order; for a single BIN, the BIN's own digest."""
        h = hashlib.sha256()
        for data in self.read_bytes(0, self.sectors * SECTOR, 4 * 1024 * 1024):
            h.update(data)
        return h.hexdigest()


@dataclass(frozen=True)
class Entry:
    path: str
    lba: int
    size: int
    directory: bool


class IsoReader:
    """ISO9660 reads limited to the data track. `source` is a DiscImage, or the
    path of a BIN whose first sector is disc LBA 0 (the data-track file)."""

    def __init__(self, source, data_end: int):
        self.owned = not isinstance(source, DiscImage)
        self.image = DiscImage.from_record(dict(bin=str(source))) if self.owned else source
        self.data_end = data_end

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.owned:
            self.image.close()

    def sector(self, lba: int) -> bytes:
        if not 0 <= lba < self.data_end:
            raise DiscError("ISO extent exceeds the data track")
        try:
            raw = self.image.read(lba)
        except DiscError:
            raw = b""
        if len(raw) != SECTOR or raw[:12] != SYNC or raw[15] != 2:
            raise DiscError(f"Invalid Mode 2 sector at LBA {lba}")
        if raw[18] & 0x20:
            raise DiscError("Form 2 sector used for an ISO file")
        return raw[24:24 + USER]

    def read(self, lba: int, size: int, limit: int = MAX_EXE) -> bytes:
        if size < 0 or size > limit:
            raise DiscError("ISO object exceeds the permitted size")
        return b"".join(self.sector(lba + i) for i in range((size + USER - 1) // USER))[:size]

    def inventory(self) -> tuple[str, list[Entry]]:
        pvd = self.sector(16)
        if pvd[:7] != b"\x01CD001\x01":
            raise DiscError("No ISO9660 primary volume descriptor")
        volume = pvd[40:72].decode("ascii").strip()
        root_lba, root_size = struct.unpack_from("<I", pvd, 158)[0], struct.unpack_from("<I", pvd, 166)[0]
        out: list[Entry] = []
        seen: set[int] = set()

        def walk(lba: int, size: int, prefix: str, depth: int):
            if lba in seen or depth > 8 or len(out) > 4096:
                raise DiscError("Cyclic or excessive ISO directory structure")
            seen.add(lba)
            data = self.read(lba, size, 1024 * 1024)
            pos = 0
            while pos < len(data):
                length = data[pos]
                if not length:
                    pos = (pos // USER + 1) * USER
                    continue
                if length < 34 or pos + length > len(data) or pos % USER + length > USER:
                    raise DiscError("Malformed ISO directory record")
                rec = data[pos:pos + length]
                pos += length
                nlen = rec[32]
                if not nlen or nlen + 33 > len(rec):
                    raise DiscError("Malformed ISO filename")
                name_bytes = rec[33:33 + nlen]
                if name_bytes in (b"\0", b"\1"):
                    continue
                name = name_bytes.decode("ascii").split(";")[0]
                if not name or name in (".", "..") or "/" in name or "\\" in name:
                    raise DiscError("Invalid ISO filename")
                extent, count = struct.unpack_from("<I", rec, 2)[0], struct.unpack_from("<I", rec, 10)[0]
                directory = bool(rec[25] & 2)
                entry = Entry(prefix + name, extent, count, directory)
                if directory:
                    walk(extent, count, entry.path + "/", depth + 1)
                else:
                    out.append(entry)
                    if len(out) > 4096:
                        raise DiscError("Too many ISO files")
        walk(root_lba, root_size, "", 0)
        return volume, out


def exe_header(data: bytes) -> dict:
    if len(data) < 2048 or data[:8] != b"PS-X EXE":
        raise DiscError("Invalid PS-X EXE header")
    entry, load, text = (struct.unpack_from("<I", data, o)[0] for o in (16, 24, 28))
    lo = load & 0x1fffffff
    if not text or text % 4 or text > MAX_EXE or text + 2048 > len(data):
        raise DiscError("Invalid PS-X EXE text size")
    if lo + text > 0x200000 or not load <= entry < load + text or entry % 4 or load % 4:
        raise DiscError("PS-X EXE addresses exceed PS1 RAM")
    return dict(entry_pc=entry, load_address=load, text_size=text,
                stack_base=struct.unpack_from("<I", data, 48)[0])


def seed_candidates(data: bytes) -> list[int]:
    h = exe_header(data)
    lo, size = h["load_address"], h["text_size"]
    seeds = {h["entry_pc"]}
    for off in range(0, size, 4):
        word = struct.unpack_from("<I", data, 2048 + off)[0]
        if word >> 26 == 3:
            target = ((lo + off + 4) & 0xf0000000) | ((word & 0x03ffffff) << 2)
            if lo <= target < lo + size:
                seeds.add(target)
    return sorted(seeds)


def inspect(cue: Path, identity: dict, progress=lambda s: None) -> dict:
    cue = cue.resolve()
    disc = read_cue(cue)
    actual_layout = [dict(number=t.number, kind=t.kind, start=t.start, pregap=t.pregap) for t in disc.tracks]
    if actual_layout != identity["tracks"]:
        raise DiscError("Unverified CUE track layout; keep every audio track")
    if disc.sectors * SECTOR != identity["bin_size"]:
        raise DiscError("Unsupported or incomplete BIN")
    progress("Verifying the complete disc, including audio tracks…")
    with DiscImage.from_disc(disc) as image:
        if image.sha256() != identity["bin_sha256"]:
            raise DiscError("Disc digest differs from the verified USA revision")
        volume, exes = _verify_data_track(image, disc.data_end, identity)
    # `bin` is the data-track BIN (the whole image for a single-BIN CUE): the
    # movie player and ISO readers address LBAs inside the data track through it.
    # `files` maps every disc sector to its BIN for audio and whole-disc reads.
    return dict(schema=1, game_id=identity["game_id"], cue=str(cue), bin=str(disc.data_file),
                files=[dict(path=str(f.path), start=f.start, sectors=f.sectors) for f in disc.files],
                bin_sha256=identity["bin_sha256"], volume=volume, tracks=actual_layout,
                executables=exes, runtime_ready=False)


def _verify_data_track(image: DiscImage, data_end: int, identity: dict) -> tuple[str, list[dict]]:
    with IsoReader(image, data_end) as iso:
        volume, entries = iso.inventory()
        by_name = {e.path: e for e in entries}
        if len(by_name) != len(entries):
            raise DiscError("Duplicate ISO filenames")
        cnf = by_name.get("SYSTEM.CNF")
        if cnf is None:
            raise DiscError("SYSTEM.CNF is missing")
        text = iso.read(cnf.lba, cnf.size, 8192).decode("ascii")
        m = re.search(r"BOOT\s*=\s*cdrom:\\?([^;\s]+)", text, re.I)
        if not m or m[1].upper() != identity["boot_exe"]:
            raise DiscError("Unexpected boot executable")
        exes = []
        for expected in identity["executables"]:
            e = by_name.get(expected["path"])
            if e is None or e.size != expected["size"]:
                raise DiscError(f"Missing or unexpected executable: {expected['path']}")
            data = iso.read(e.lba, e.size)
            if hashlib.sha256(data).hexdigest() != expected["sha256"]:
                raise DiscError(f"Executable verification failed: {e.path}")
            h = exe_header(data)
            exes.append(dict(path=e.path, size=e.size, sha256=expected["sha256"],
                             **h, seed_candidates=len(seed_candidates(data))))
    return volume, exes


def prepare(cue: Path, destination: Path, identity: dict, progress=lambda s: None) -> dict:
    # Verify before creating a destination. Existing installations are never replaced.
    result = inspect(cue, identity, progress)
    destination = destination.resolve()
    if destination.exists():
        raise DiscError(f"Destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".n2o-import-", dir=destination.parent))
    try:
        (staging / "executables").mkdir()
        (staging / "seeds").mkdir()
        progress("Preparing local executable inputs…")
        data_end = result["tracks"][1]["pregap"] or result["tracks"][1]["start"]
        with DiscImage.from_record(result) as image, IsoReader(image, data_end) as iso:
            _, entries = iso.inventory()
            files = {e.path: e for e in entries}
            for e in result["executables"]:
                source = files[e["path"]]
                data = iso.read(source.lba, source.size)
                if hashlib.sha256(data).hexdigest() != e["sha256"]:
                    raise DiscError("Disc changed while preparing local files")
                (staging / "executables" / e["path"]).write_bytes(data)
                seeds = seed_candidates(data)
                (staging / "seeds" / (e["path"] + ".txt")).write_text(
                    "".join(f"0x{s:08X}\n" for s in seeds), encoding="utf-8")
        # Only paths/hashes/analysis metadata go in the manifest, never embedded bytes.
        (staging / "installation.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        if destination.exists():
            raise DiscError("Destination was created during preparation")
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return result


def default_data_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "NitrogenSquared"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "nitrogen-squared"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("cue", type=Path)
    p.add_argument("--prepare", type=Path, help="New private local data directory; never a release folder")
    args = p.parse_args()
    identity = json.loads((ROOT / "configs/disc_usa.json").read_text())
    try:
        result = prepare(args.cue, args.prepare, identity, print) if args.prepare else inspect(args.cue, identity, print)
    except (DiscError, OSError, UnicodeError) as e:
        p.exit(1, f"Disc preparation failed: {e}\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
