"""GPL-3.0-or-later. CD-DA audio validation and conversion for soundtrack packages.

The framework's psx_mod_append_cdda_tracks() plays verified resources as raw
2352-byte CD-DA sectors: interleaved stereo, signed 16-bit little-endian
samples at 44.1 kHz, with no header. N2O's own audio tracks have exactly this
layout; the asset extractor's WAVE files contain the disc sectors byte for byte.

Only uncompressed PCM WAVE files already in that format are accepted. Nothing is
resampled, re-quantized, remixed or dithered: conversion copies the sample bytes
unchanged and pads the final sector with digital silence.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import hashlib
import struct

from . import ModError

SECTOR = 2352                       # bytes in one CD-DA sector (588 stereo frames)
RATE, CHANNELS, BITS = 44100, 2, 16
FRAME = CHANNELS * BITS // 8        # 4 bytes per stereo frame
BYTES_PER_SECOND = RATE * FRAME     # 176,400
FORMAT_LABEL = "PCM 44.1 kHz, 16-bit, stereo"
# A package expands to at most 256 MiB (framework installer limit). Leave room
# for the manifest and playlist so a single track can never make export fail.
MAX_PACKAGE_BYTES = 256 * 1024 * 1024
MAX_TRACK_SECTORS = (MAX_PACKAGE_BYTES - 1024 * 1024) // SECTOR
MIN_TRACK_SECTORS = 75              # one second; shorter files are almost certainly mistakes
MAX_WAVE_BYTES = MAX_TRACK_SECTORS * SECTOR + 64 * 1024 * 1024
_PCM, _EXTENSIBLE = 0x0001, 0xFFFE
_PCM_GUID = bytes.fromhex("0100000000001000800000aa00389b71")


@dataclass(frozen=True)
class WaveInfo:
    path: Path
    data_offset: int
    data_bytes: int

    @property
    def frames(self) -> int:
        return self.data_bytes // FRAME

    @property
    def sectors(self) -> int:
        return -(-self.data_bytes // SECTOR)

    @property
    def seconds(self) -> float:
        return self.data_bytes / BYTES_PER_SECOND


def sectors_seconds(sectors: int) -> float:
    return sectors * SECTOR / BYTES_PER_SECOND


def read_wave(path: Path) -> WaveInfo:
    """Validate a RIFF/WAVE file as CD-DA-compatible PCM without loading samples."""
    path = Path(path)
    try:
        size = path.stat().st_size
        handle = path.open("rb")
    except OSError as e:
        raise ModError(f"Cannot read {path.name}: {e.strerror or e}") from None
    with handle:
        if size > MAX_WAVE_BYTES:
            raise ModError(f"{path.name} is too large for a soundtrack package")
        header = handle.read(12)
        if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise ModError(f"{path.name} is not a RIFF/WAVE file")
        fmt = None
        data = None
        position = 12
        while position + 8 <= size:
            handle.seek(position)
            chunk, length = struct.unpack("<4sI", handle.read(8))
            body = position + 8
            if body + length > size:
                raise ModError(f"{path.name} is truncated (chunk {chunk.decode('latin-1', 'replace')!r})")
            if chunk == b"fmt ":
                if length < 16:
                    raise ModError(f"{path.name} has an invalid format chunk")
                fmt = handle.read(length)
            elif chunk == b"data":
                if data is not None:
                    raise ModError(f"{path.name} has more than one data chunk")
                data = (body, length)
            position = body + length + (length & 1)   # chunks are word aligned
        if fmt is None or data is None:
            raise ModError(f"{path.name} lacks a format or data chunk")
    tag, channels, rate, byte_rate, align, bits = struct.unpack("<HHIIHH", fmt[:16])
    if tag == _EXTENSIBLE:
        if len(fmt) < 40 or fmt[24:40] != _PCM_GUID:
            raise ModError(f"{path.name} is not uncompressed PCM")
        valid_bits = struct.unpack("<H", fmt[18:20])[0]
        if valid_bits != BITS:
            raise ModError(f"{path.name} has {valid_bits} valid bits per sample; use 16-bit PCM")
    elif tag != _PCM:
        raise ModError(f"{path.name} is compressed or not PCM (format {tag:#06x}); export it as 16-bit PCM WAV")
    problems = []
    if rate != RATE:
        problems.append(f"sample rate {rate} Hz (needs 44100 Hz)")
    if bits != BITS:
        problems.append(f"{bits}-bit samples (needs 16-bit)")
    if channels != CHANNELS:
        problems.append(f"{channels} channel(s) (needs stereo)")
    if problems:
        raise ModError(f"{path.name} uses " + ", ".join(problems) + ". Convert it first; Nitrogen does not resample.")
    if align != FRAME or byte_rate != BYTES_PER_SECOND:
        raise ModError(f"{path.name} has an inconsistent PCM header")
    offset, length = data
    if length % FRAME:
        raise ModError(f"{path.name} ends with a partial sample frame")
    info = WaveInfo(path, offset, length)
    if info.sectors < MIN_TRACK_SECTORS:
        raise ModError(f"{path.name} is shorter than one second")
    if info.sectors > MAX_TRACK_SECTORS:
        limit = sectors_seconds(MAX_TRACK_SECTORS) / 60
        raise ModError(f"{path.name} lasts {info.seconds / 60:.1f} minutes; a track can be at most {limit:.1f} minutes")
    return info


def convert(info: WaveInfo, destination: Path) -> dict:
    """Write raw CD-DA sectors (samples unchanged, final sector zero-padded)."""
    destination = Path(destination)
    temporary = destination.with_name(destination.name + ".tmp")
    digest = hashlib.sha256()
    remaining = info.data_bytes
    with info.path.open("rb") as source, temporary.open("wb") as out:
        source.seek(info.data_offset)
        while remaining:
            chunk = source.read(min(remaining, 1024 * 1024))
            if not chunk:
                temporary.unlink(missing_ok=True)
                raise ModError(f"{info.path.name} changed while it was being converted")
            out.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        padding = info.sectors * SECTOR - info.data_bytes
        out.write(bytes(padding))
        digest.update(bytes(padding))
    temporary.replace(destination)
    return dict(sectors=info.sectors, size=info.sectors * SECTOR, sha256=digest.hexdigest(),
                padding=padding, seconds=round(info.seconds, 3))


def write_wave(destination: Path, chunks, data_bytes: int) -> None:
    """A minimal PCM WAVE for previews: 44.1 kHz, 16-bit stereo."""
    with Path(destination).open("wb") as out:
        out.write(struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + data_bytes, b"WAVE", b"fmt ", 16, _PCM,
                              CHANNELS, RATE, BYTES_PER_SECOND, FRAME, BITS, b"data", data_bytes))
        for chunk in chunks:
            out.write(chunk)
