"""Single- and multi-BIN CUE layouts built from original synthetic sectors; no game content."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from n2o_disc import SECTOR, DiscError, DiscImage, IsoReader, inspect, prepare, read_cue
from extract_assets import extract
from modkit.soundtrack import SLOTS, SoundtrackProject, original_tracks
from portable_launcher import select_cue
from test_disc import fixture

DATA_SECTORS = 60          # test_disc's ISO data track ends at LBA 60
PREGAP = 150               # 00:02:00, as in per-track Redump-style dumps


def msf(n):
    return f"{n // 4500:02d}:{n // 75 % 60:02d}:{n % 75:02d}"


def audio_sectors(number):
    """Distinct original audio per track; some sectors begin with a Mode 2 sync
    pattern so a reader that treated audio as data would misread them."""
    out = bytearray()
    for i in range(4 + number):
        sector = bytearray(bytes([number, i]) * (SECTOR // 2))
        if i % 2 == 0:
            sector[:12] = b"\x00" + b"\xff" * 10 + b"\x00"
            sector[15] = 2
        out += sector
    return bytes(out)


class Disc:
    """An original 11-track mixed-mode disc: data Track 01 plus ten audio tracks."""

    def __init__(self, root: Path):
        self.root = root
        (root / "base").mkdir()
        _, base = fixture(root / "base")
        self.data = (root / "base/synthetic.bin").read_bytes()[:DATA_SECTORS * SECTOR]
        self.pregaps = {n: bytes([0x80 + n]) * (PREGAP * SECTOR) for n in range(2, 12)}
        self.audio = {n: audio_sectors(n) for n in range(2, 12)}
        merged, tracks = bytearray(self.data), [dict(number=1, kind="MODE2/2352", start=0, pregap=None)]
        for n in range(2, 12):
            pregap = len(merged) // SECTOR
            merged += self.pregaps[n] + self.audio[n]
            tracks.append(dict(number=n, kind="AUDIO", start=pregap + PREGAP, pregap=pregap))
        self.merged, self.tracks = bytes(merged), tracks
        self.identity = dict(base, bin_size=len(self.merged), bin_sha256=hashlib.sha256(self.merged).hexdigest(),
                             tracks=tracks)

    def merged_cue(self, folder="merged"):
        d = self.root / folder
        d.mkdir()
        (d / "Disc.bin").write_bytes(self.merged)
        lines = ['FILE "Disc.bin" BINARY', "  TRACK 01 MODE2/2352", "    INDEX 01 00:00:00"]
        for t in self.tracks[1:]:
            lines += [f"  TRACK {t['number']:02d} AUDIO", f"    INDEX 00 {msf(t['pregap'])}", f"    INDEX 01 {msf(t['start'])}"]
        (d / "Disc.cue").write_text("\n".join(lines) + "\n")
        return d / "Disc.cue"

    def split_cue(self, folder="split", newline="\r\n"):
        """One BIN per track, each audio BIN starting with its INDEX 00 pregap, like the owner's dump."""
        d = self.root / folder
        d.mkdir()
        (d / "Disc (Track 01).bin").write_bytes(self.data)
        lines = ['FILE "Disc (Track 01).bin" BINARY', "  TRACK 01 MODE2/2352", "    INDEX 01 00:00:00"]
        for n in range(2, 12):
            (d / f"Disc (Track {n:02d}).bin").write_bytes(self.pregaps[n] + self.audio[n])
            lines += [f'FILE "Disc (Track {n:02d}).bin" BINARY', f"  TRACK {n:02d} AUDIO",
                      "    INDEX 00 00:00:00", f"    INDEX 01 {msf(PREGAP)}"]
        (d / "Disc.cue").write_bytes((newline.join(lines) + newline).encode())
        return d / "Disc.cue"

    def write(self, folder, cue_text, files):
        d = self.root / folder
        d.mkdir(parents=True)
        for name, data in files.items():
            (d / name).parent.mkdir(parents=True, exist_ok=True)
            (d / name).write_bytes(data)
        (d / "Disc.cue").write_text(cue_text)
        return d / "Disc.cue"


class CueLayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.disc = Disc(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_eleven_bin_cue_follows_index_metadata_to_the_merged_layout(self):
        split, merged = read_cue(self.disc.split_cue()), read_cue(self.disc.merged_cue())
        self.assertEqual(len(split.files), 11)
        self.assertEqual(split.track_files, tuple(range(11)))
        self.assertEqual([f.start for f in split.files],
                         [0] + [t["pregap"] for t in self.disc.tracks[1:]])
        self.assertEqual([f.sectors for f in split.files],
                         [DATA_SECTORS] + [PREGAP + 4 + n for n in range(2, 12)])
        self.assertEqual(split.tracks, merged.tracks)
        self.assertEqual([dict(number=t.number, kind=t.kind, start=t.start, pregap=t.pregap) for t in split.tracks],
                         self.disc.tracks)
        self.assertEqual((split.sectors, split.data_end), (merged.sectors, merged.data_end))
        self.assertEqual(split.data_file.name, "Disc (Track 01).bin")

    def test_eleven_bin_import_verifies_the_same_identity_without_merging(self):
        split_cue = self.disc.split_cue()
        before = {p.name: p.read_bytes() for p in split_cue.parent.iterdir()}
        result = prepare(split_cue, self.root / "installation", self.disc.identity)
        self.assertEqual({p.name: p.read_bytes() for p in split_cue.parent.iterdir()}, before)  # originals only read
        self.assertEqual(Path(result["bin"]).name, "Disc (Track 01).bin")
        self.assertEqual([Path(f["path"]).name for f in result["files"]], sorted(before)[:-1])
        self.assertEqual(sum(f["sectors"] for f in result["files"]) * SECTOR, len(self.disc.merged))
        self.assertEqual(result["tracks"], self.disc.tracks)
        self.assertFalse(list((self.root / "installation").rglob("*.bin")))   # no merged copy is written
        merged = inspect(self.disc.merged_cue(), self.disc.identity)
        self.assertEqual(len(merged["files"]), 1)
        self.assertEqual(merged["bin"], merged["files"][0]["path"])         # single-BIN records are unchanged
        self.assertEqual((merged["bin_sha256"], merged["executables"]), (result["bin_sha256"], result["executables"]))

    def test_disc_image_reads_across_bin_boundaries_and_hashes_the_disc(self):
        disc = read_cue(self.disc.split_cue())
        with DiscImage.from_disc(disc) as image:
            self.assertEqual(image.sha256(), self.disc.identity["bin_sha256"])
            boundary = disc.files[3].start
            self.assertEqual(image.read(boundary - 2, 5), self.disc.merged[(boundary - 2) * SECTOR:(boundary + 3) * SECTOR])
            self.assertEqual(b"".join(image.read_bytes(boundary * SECTOR - 7, 20)),
                             self.disc.merged[boundary * SECTOR - 7:boundary * SECTOR + 13])
            with self.assertRaises(DiscError):
                image.read(disc.sectors - 1, 2)

    def test_tracks_sharing_a_bin_and_tracks_split_over_bins(self):
        d, t = self.disc, self.disc.tracks
        first = d.data + d.pregaps[2] + d.audio[2]                           # data + track 2
        second = d.pregaps[3] + d.audio[3] + d.pregaps[4] + d.audio[4]       # tracks 3 and 4
        rest = b"".join(d.pregaps[n] + d.audio[n] for n in range(5, 12))     # tracks 5-11
        b3, b5 = len(first) // SECTOR, (len(first) + len(second)) // SECTOR
        lines = ['FILE "a.bin" BINARY', " TRACK 01 MODE2/2352", "  INDEX 01 00:00:00",
                 " TRACK 02 AUDIO", f"  INDEX 00 {msf(t[1]['pregap'])}", f"  INDEX 01 {msf(t[1]['start'])}",
                 'FILE "b.bin" BINARY']
        for n in (3, 4):
            lines += [f" TRACK {n:02d} AUDIO", f"  INDEX 00 {msf(t[n - 1]['pregap'] - b3)}", f"  INDEX 01 {msf(t[n - 1]['start'] - b3)}"]
        lines.append('FILE "c.bin" BINARY')
        for n in range(5, 12):
            lines += [f" TRACK {n:02d} AUDIO", f"  INDEX 00 {msf(t[n - 1]['pregap'] - b5)}", f"  INDEX 01 {msf(t[n - 1]['start'] - b5)}"]
        cue = d.write("shared", "\n".join(lines) + "\n", {"a.bin": first, "b.bin": second, "c.bin": rest})
        disc = read_cue(cue)
        self.assertEqual(disc.track_files, (0, 0, 1, 1, 2, 2, 2, 2, 2, 2, 2))
        self.assertEqual([dict(number=x.number, kind=x.kind, start=x.start, pregap=x.pregap) for x in disc.tracks], t)
        self.assertEqual(inspect(cue, d.identity)["tracks"], t)

    def test_data_track_reads_never_enter_audio_and_audio_is_never_data(self):
        disc = read_cue(self.disc.split_cue())
        with DiscImage.from_disc(disc) as image, IsoReader(image, disc.data_end) as iso:
            self.assertEqual(iso.inventory()[0], "SYNTHETIC")
            with self.assertRaisesRegex(DiscError, "exceeds the data track"):
                iso.sector(disc.data_end + PREGAP)       # sync-looking audio sector
        audio_first = self.disc.write("audio-first", 'FILE "a.bin" BINARY\n TRACK 01 AUDIO\n  INDEX 01 00:00:00\n',
                                      {"a.bin": self.disc.audio[2]})
        with self.assertRaisesRegex(DiscError, "Track 1 must start at sector zero in MODE2/2352"):
            read_cue(audio_first)
        text = self.disc.split_cue("data-later").read_text().replace("TRACK 03 AUDIO", "TRACK 03 MODE2/2352")
        (self.root / "data-later/Disc.cue").write_text(text)
        with self.assertRaisesRegex(DiscError, "Expected audio tracks after Track 1"):
            read_cue(self.root / "data-later/Disc.cue")

    def test_soundtrack_and_asset_reads_from_an_eleven_bin_import_match_the_merged_disc(self):
        installs = {}
        for name, cue in (("split", self.disc.split_cue()), ("merged", self.disc.merged_cue())):
            installs[name] = self.root / f"install-{name}"
            prepare(cue, installs[name], self.disc.identity)
        tracks = {k: original_tracks(v) for k, v in installs.items()}
        self.assertEqual({n: v[1:] for n, v in tracks["split"].items()}, {n: v[1:] for n, v in tracks["merged"].items()})
        self.assertEqual(tracks["split"][2][2], 4 + 2)                      # INDEX 01 to next INDEX 00: no pregap
        previews = {}
        for name, install in installs.items():
            project = SoundtrackProject(self.root / f"project-{name}", install, game_id="TEST")
            previews[name] = b"".join(project._source_chunks(dict(kind="disc", track=7), tracks[name], 100, 5 * SECTOR))
        self.assertEqual(previews["split"], self.disc.audio[7][100:100 + 5 * SECTOR])
        self.assertEqual(previews["split"], previews["merged"])
        manifests = {k: extract(v, self.root / f"assets-{k}", self.disc.identity, lambda _: None) for k, v in installs.items()}
        self.assertEqual(manifests["split"]["outputs"], manifests["merged"]["outputs"])
        self.assertEqual([m["track"] for m in manifests["split"]["music"]], list(SLOTS))
        pregap = self.root / "assets-split/disc/pregaps/track-05.pcm"
        self.assertEqual(pregap.read_bytes(), self.disc.pregaps[5])

    def test_records_from_before_multi_bin_support_still_read_their_bin(self):
        install = self.root / "install"
        prepare(self.disc.merged_cue(), install, self.disc.identity)
        record = json.loads((install / "installation.json").read_text())
        del record["files"]
        (install / "installation.json").write_text(json.dumps(record))
        with DiscImage.from_record(record) as image:
            self.assertEqual(image.sha256(), self.disc.identity["bin_sha256"])
        self.assertEqual(original_tracks(install)[11][1:], (self.disc.tracks[10]["start"], 4 + 11))

    def test_any_bin_of_a_multi_bin_cue_selects_that_cue(self):
        cue = self.disc.split_cue()
        for n in (1, 2, 11):
            self.assertEqual(select_cue(cue.parent / f"Disc (Track {n:02d}).bin"), cue.resolve())
        (cue.parent / "Unrelated.bin").write_bytes(bytes(SECTOR))
        with self.assertRaisesRegex(ValueError, "all of its BIN files together"):
            select_cue(cue.parent / "Unrelated.bin")

    def test_relative_subfolder_paths_and_letter_case_resolve_beside_the_cue(self):
        text = self.disc.split_cue("sub").read_text().replace('FILE "Disc (Track 0', 'FILE "Tracks\\disc (track 0')
        (self.root / "sub/Tracks").mkdir()
        for n in range(1, 10):
            (self.root / f"sub/Disc (Track {n:02d}).bin").rename(self.root / f"sub/Tracks/Disc (Track {n:02d}).bin")
        (self.root / "sub/Disc.cue").write_text(text)
        disc = read_cue(self.root / "sub/Disc.cue")
        self.assertEqual(disc.files[0].path, (self.root / "sub/Tracks/Disc (Track 01).bin").resolve())
        self.assertEqual(disc.tracks, read_cue(self.disc.merged_cue()).tracks)

    def test_malformed_or_unsupported_layouts_name_the_problem(self):
        good = self.disc.split_cue("errors").read_text()
        cue = self.root / "errors/Disc.cue"
        one = 'FILE "Disc (Track 01).bin" BINARY\n TRACK 01 MODE2/2352\n  INDEX 01 00:00:00\n'
        cases = [
            ("missing BIN", good.replace("Track 05).bin", "Track 05) missing.bin"), r"missing: Disc \(Track 05\) missing\.bin"),
            ("wave file", good.replace("(Track 03).bin\" BINARY", "(Track 03).bin\" WAVE"), "only raw BINARY"),
            ("cooked sectors", good.replace("TRACK 01 MODE2/2352", "TRACK 01 MODE1/2048"), "2,352-byte sectors"),
            ("virtual pregap", good.replace("TRACK 04 AUDIO", "TRACK 04 AUDIO\n    PREGAP 00:02:00"), "PREGAP adds sectors"),
            ("index across files", one + 'FILE "Disc (Track 02).bin" BINARY\n  INDEX 01 00:02:00\n', "before any TRACK|continues into another FILE"),
            ("index across files 2", one + ' TRACK 02 AUDIO\n  INDEX 00 00:00:10\nFILE "Disc (Track 02).bin" BINARY\n  INDEX 01 00:00:00\n',
             "continues into another FILE"),
            ("file without track", one + 'FILE "Disc (Track 02).bin" BINARY\n', "has no TRACK"),
            ("duplicate file", one + 'FILE "Disc (Track 01).bin" BINARY\n TRACK 02 AUDIO\n  INDEX 01 00:00:00\n', "more than once"),
            ("numbering", good.replace("TRACK 06 AUDIO", "TRACK 07 AUDIO", 1), "numbered"),
            ("index past end", good.replace("INDEX 01 00:02:00", "INDEX 01 01:00:00", 1), "outside or overlaps"),
            ("track before file", " TRACK 01 MODE2/2352\n" + good, "before any FILE"),
            ("no index 01", 'FILE "Disc (Track 01).bin" BINARY\n TRACK 01 MODE2/2352\n  INDEX 00 00:00:00\n', "no INDEX 01"),
            ("index order", good.replace("INDEX 00 00:00:00\n    INDEX 01 00:02:00", "INDEX 01 00:02:00\n    INDEX 00 00:00:00", 1), "out of order"),
            ("parent path", good.replace('"Disc (Track 02).bin"', '"../Disc (Track 02).bin"'), "relative to the CUE folder"),
            ("absolute path", good.replace('"Disc (Track 02).bin"', '"C:\\Disc (Track 02).bin"'), "relative to the CUE folder"),
            ("unknown directive", good + "CDTEXTFILE \"x.cdt\"\n", "Unsupported CUE directive"),
            ("malformed file", "FILE\n" + good, "Malformed CUE FILE"),
        ]
        for name, text, message in cases:
            with self.subTest(name):
                cue.write_text(text)
                with self.assertRaisesRegex(DiscError, message):
                    read_cue(cue)
        cue.write_text(good)
        (self.root / "errors/Disc (Track 08).bin").write_bytes(b"x" * (SECTOR * 3 + 1))
        with self.assertRaisesRegex(DiscError, r"Disc \(Track 08\)\.bin is not a whole number"):
            read_cue(cue)
        (self.root / "errors/Disc (Track 08).bin").write_bytes(self.disc.pregaps[8] + self.disc.audio[8])
        with patch("n2o_disc.open", create=True, side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaisesRegex(DiscError, r"cannot be read: Disc \(Track 01\)\.bin \(Permission denied\)"):
                read_cue(cue)
        read_cue(cue)                                                       # restored layout is accepted
        cue.write_text("REM comment\nCATALOG 0000000000000\nTITLE \"Synthetic\"\n" + good.replace("  TRACK", "\tTRACK\t"))
        self.assertEqual(read_cue(cue).tracks, read_cue(self.disc.merged_cue()).tracks)   # metadata and tabs

    def test_a_changed_or_missing_track_bin_is_refused_before_import(self):
        cue = self.disc.split_cue()
        target = cue.parent / "Disc (Track 09).bin"
        data = bytearray(target.read_bytes())
        data[-1] ^= 1
        target.write_bytes(data)
        with self.assertRaisesRegex(DiscError, "digest"):
            prepare(cue, self.root / "rejected", self.disc.identity)
        target.unlink()
        with self.assertRaisesRegex(DiscError, "missing"):
            prepare(cue, self.root / "rejected", self.disc.identity)
        self.assertFalse((self.root / "rejected").exists())


if __name__ == "__main__":
    unittest.main()
