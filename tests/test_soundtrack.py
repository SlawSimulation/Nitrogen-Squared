"""Original synthetic soundtrack checks: WAVE validation, CD-DA conversion,
ten-slot editing, previews, export and verified-resource staging.

A synthetic disc image of silent and patterned sectors stands in for the
player's disc; no game data is used."""
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 uses the documented tomli package.
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from modkit import ModError  # noqa: E402
from modkit import audio  # noqa: E402
from modkit.package import ModStore  # noqa: E402
from modkit.soundtrack import SoundtrackProject, SLOTS, PLAYLIST_HEADER, check_package  # noqa: E402

SECTOR = 2352


def wave_bytes(frames=44100, rate=44100, channels=2, bits=16, tag=1, data=None, extra_chunks=b"", odd_pad=False):
    align = channels * bits // 8
    body = data if data is not None else bytes((i * 7) % 251 for i in range(frames * align))
    fmt = struct.pack("<HHIIHH", tag, channels, rate, rate * align, align, bits)
    if tag == 0xFFFE:
        fmt += struct.pack("<HHI", 22, bits, 3) + bytes.fromhex("0100000000001000800000aa00389b71")
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt + extra_chunks + b"data" + struct.pack("<I", len(body)) + body
    if odd_pad:
        chunks += b"\0"
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


def fake_installation(root: Path) -> Path:
    """A synthetic merged image: data track, then ten short audio tracks with
    pregaps. Track n's samples are the byte value n, so slices are identifiable."""
    image = bytearray(SECTOR * 100)  # data track
    tracks = [dict(number=1, kind="MODE2/2352", start=0, pregap=None)]
    for n in SLOTS:
        pregap = len(image) // SECTOR
        image += bytes(SECTOR * 2)                       # INDEX 00 pregap
        start = len(image) // SECTOR
        image += bytes([n]) * (SECTOR * (75 + n))        # 1 s plus n sectors of audio
        tracks.append(dict(number=n, kind="AUDIO", start=start, pregap=pregap))
    installation = root / "installation"
    installation.mkdir()
    (root / "disc.bin").write_bytes(bytes(image))
    (installation / "installation.json").write_text(json.dumps(dict(bin=str(root / "disc.bin"), tracks=tracks)))
    return installation


class WaveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data):
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_cd_compatible_pcm_is_accepted_and_converted_unchanged(self):
        frames = 44100 + 100                     # not a whole number of sectors
        payload = bytes((i * 13) % 256 for i in range(frames * 4))
        path = self.write("ok.wav", wave_bytes(data=payload, extra_chunks=b"LIST\x05\0\0\0abcde\0", odd_pad=True))
        info = audio.read_wave(path)
        self.assertEqual((info.frames, info.sectors), (frames, -(-frames * 4 // SECTOR)))
        self.assertAlmostEqual(info.seconds, frames / 44100)
        result = audio.convert(info, self.root / "out.pcm")
        out = (self.root / "out.pcm").read_bytes()
        self.assertEqual(len(out) % SECTOR, 0)
        self.assertEqual(out[:len(payload)], payload)               # samples byte-identical
        self.assertEqual(out[len(payload):], bytes(result["padding"]))  # silence padding only
        self.assertEqual(result["sha256"], hashlib.sha256(out).hexdigest())
        extensible = self.write("ext.wav", wave_bytes(tag=0xFFFE))
        self.assertEqual(audio.read_wave(extensible).frames, 44100)

    def test_unsupported_audio_is_refused_with_a_reason(self):
        cases = {
            "48 kHz": (wave_bytes(rate=48000), "sample rate 48000"),
            "8-bit": (wave_bytes(bits=8), "8-bit"),
            "24-bit": (wave_bytes(bits=24), "24-bit"),
            "mono": (wave_bytes(channels=1), "1 channel"),
            "float": (wave_bytes(tag=3, bits=32), "not PCM"),
            "compressed": (wave_bytes(tag=2), "not PCM"),
            "short": (wave_bytes(frames=1000), "shorter than one second"),
            "partial frame": (wave_bytes(data=bytes(44100 * 4 + 2)), "partial sample frame"),
            "not riff": (b"OggS" + bytes(100), "not a RIFF/WAVE"),
            "truncated": (wave_bytes()[:-10000], "truncated"),
            "no data": (wave_bytes()[:36], "lacks"),
        }
        for label, (data, message) in cases.items():
            with self.subTest(label=label), self.assertRaisesRegex(ModError, message):
                audio.read_wave(self.write(label.replace(" ", "_") + ".wav", data))

    def test_duration_limit_is_the_package_budget(self):
        path = self.write("long.wav", wave_bytes(frames=44100 * 2))
        with patch.object(audio, "MAX_TRACK_SECTORS", 100):
            with self.assertRaisesRegex(ModError, "at most"):
                audio.read_wave(path)
        self.assertLess(audio.sectors_seconds(audio.MAX_TRACK_SECTORS), 26 * 60)
        self.assertGreater(audio.sectors_seconds(audio.MAX_TRACK_SECTORS), 25 * 60)


class ProjectCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.installation = fake_installation(self.root)
        self.project = SoundtrackProject(self.root / "soundtrack", self.installation)
        self.wave = self.root / "tone.wav"
        self.wave.write_bytes(wave_bytes(frames=44100 * 2))

    def tearDown(self):
        self.tmp.cleanup()


class ProjectTests(ProjectCase):
    def test_original_tracks_exclude_pregaps(self):
        status = self.project.status()
        self.assertEqual([r["slot"] for r in status["slots"]], list(SLOTS))
        self.assertEqual([r["sectors"] for r in status["slots"]], [75 + n for n in SLOTS])
        self.assertFalse(status["changed"])
        self.assertEqual({r["state"] for r in status["slots"]}, {"original"})

    def test_replace_move_restore_reset_and_cleanup(self):
        before = self.wave.read_bytes()
        self.project.import_wave(3, self.wave)
        self.assertEqual(self.wave.read_bytes(), before)        # the source file is never changed
        status = self.project.status()
        self.assertEqual(status["slots"][1]["state"], "replacement")
        self.assertEqual(status["replacement_bytes"], -(-44100 * 2 * 4 // SECTOR) * SECTOR)
        self.project.move(3, -1)
        rows = self.project.status()["slots"]
        self.assertEqual((rows[0]["state"], rows[1]["state"], rows[1]["track"]), ("replacement", "moved", 2))
        for slot, offset in ((2, -1), (11, 1), (5, 2)):
            with self.subTest(slot=slot, offset=offset), self.assertRaises(ModError):
                self.project.move(slot, offset)
        with self.assertRaises(ModError):
            self.project.import_wave(12, self.wave)
        self.project.restore(2)
        self.assertEqual(list((self.project.replacements).glob("*.pcm")), [])  # unused audio removed
        self.project.reset()
        self.assertFalse(self.project.status()["changed"])

    def test_previews_read_disc_slices_and_replacements(self):
        import wave
        self.project.import_wave(4, self.wave)
        preview = self.project.preview(2)
        with wave.open(str(preview)) as w:
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth()), (44100, 2, 2))
            self.assertEqual(set(w.readframes(w.getnframes())), {2})   # track 2's bytes, no pregap
        playlist = self.project.preview(None)
        with wave.open(str(playlist)) as w:
            data = w.readframes(w.getnframes())
        self.assertEqual(data[:SECTOR * 77], bytes([2]) * SECTOR * 77)
        self.assertEqual(data[SECTOR * 77:SECTOR * 155], bytes([3]) * SECTOR * 78)
        self.assertEqual(len(list(self.project.previews.glob("*.wav"))), 1)  # older previews removed

    def test_budget_and_details(self):
        with patch("modkit.audio.MAX_PACKAGE_BYTES", 1024 * 1024 + 100000):  # 100 kB budget
            with self.assertRaisesRegex(ModError, "one package holds"):
                self.project.import_wave(2, self.wave)
        for details in ({"id": "Bad Id"}, {"version": "1"}, {"name": " "}):
            with self.subTest(details=details), self.assertRaises(ModError):
                self.project.set_details(details)
        self.assertEqual(self.project.set_details({"id": "tester.tones", "author": "Me"})["author"], "Me")


class ExportTests(ProjectCase):
    def export(self):
        return Path(self.project.export(None)["path"])

    def test_nothing_changed_cannot_export(self):
        with self.assertRaisesRegex(ModError, "at least one"):
            self.project.export(None)

    def test_export_contains_only_replacements_and_a_valid_playlist(self):
        self.project.import_wave(5, self.wave)
        self.project.import_wave(9, self.wave)       # same audio twice: stored once
        self.project.move(3, 1)                       # slot 3 <- disc 4, slot 4 <- disc 3
        path = self.export()
        with zipfile.ZipFile(path) as z:
            names = sorted(z.namelist())
            manifest = tomllib.loads(z.read("manifest.toml").decode())
            playlist = z.read("resources/playlist.txt").decode().splitlines()
            pcm = z.read("resources/audio-1.pcm")
        self.assertEqual(names, ["README.txt", "manifest.toml", "resources/audio-1.pcm", "resources/playlist.txt"])
        self.assertEqual(playlist[0], PLAYLIST_HEADER)
        self.assertEqual(playlist[1:], ["2 disc 2", "3 disc 4", "4 disc 3", "5 resource audio-1", "6 disc 6",
                                        "7 disc 7", "8 disc 8", "9 resource audio-1", "10 disc 10", "11 disc 11"])
        self.assertNotIn(bytes([2]) * 64, pcm)                 # no disc audio copied
        self.assertEqual(manifest["plugin"], [{"feature": "soundtrack", "id": "nitrogen.soundtrack"}])
        resources = {r["id"]: r for r in manifest["resource"]}
        self.assertEqual(set(resources), {"playlist", "audio-1"})
        self.assertEqual(resources["audio-1"]["sha256"], hashlib.sha256(pcm).hexdigest())
        self.assertTrue(all(r["required"] and r["format"] == "file" for r in resources.values()))
        self.assertFalse(manifest["feature"][0]["default_enabled"])

    def test_tampered_export_is_refused(self):
        self.project.import_wave(5, self.wave)
        path = self.export()
        bad = self.root / "bad.psxmod"
        with zipfile.ZipFile(path) as src, zipfile.ZipFile(bad, "w") as dst:
            for item in src.infolist():
                data = src.read(item)
                if item.filename == "resources/audio-1.pcm":
                    data = data[:-1] + b"\x01"
                dst.writestr(item, data)
        with self.assertRaisesRegex(ModError, "does not match"):
            check_package(bad)

    def test_export_installs_and_stages_verified_resources(self):
        self.project.import_wave(5, self.wave)
        path = self.export()
        store = ModStore(self.root / "mods")
        record = store.install(path, None)
        self.assertEqual(record["bound_resources"], {"playlist": "resources/playlist.txt",
                                                     "audio-1": "resources/audio-1.pcm"})
        [status] = store.status(None)
        self.assertEqual(status["compatibility"]["state"], "unchecked")
        exe = self.root / "build"
        exe.mkdir()
        build = (exe / "game.exe", "sha")
        with patch("modkit.validator._audit", return_value=(
                "mod plugin audit: 1 manifest(s), 1 declared id(s), 1 registered id(s)\n")) as audit, \
                patch("modkit.validator.registered_plugins", return_value=["nitrogen.soundtrack"]):
            store.set_enabled(record["id"], True, build)
            store.stage(exe, build)
        state = tomllib.loads((exe / "mods/state.toml").read_text())
        paths = state["feature"][0]["resources"]
        self.assertEqual(set(paths), {"playlist", "audio-1"})
        staged = Path(paths["audio-1"])
        self.assertTrue(staged.is_absolute() and staged.is_relative_to((exe / "mods/installed").resolve()))
        self.assertEqual(staged.read_bytes(), zipfile.ZipFile(path).read("resources/audio-1.pcm"))
        audit.assert_called()

    def test_package_missing_a_required_shipped_resource_needs_files(self):
        self.project.import_wave(5, self.wave)
        path = self.export()
        stripped = self.root / "stripped.psxmod"
        with zipfile.ZipFile(path) as src, zipfile.ZipFile(stripped, "w") as dst:
            for item in src.infolist():
                if item.filename != "resources/audio-1.pcm":
                    dst.writestr(item, src.read(item))
        store = ModStore(self.root / "mods")
        store.install(stripped, None)
        [status] = store.status(None)
        self.assertEqual(status["compatibility"]["state"], "needs-files")
        with self.assertRaisesRegex(ModError, "cannot be enabled"):
            store.set_enabled(status["id"], True, None)


class LauncherServiceTests(unittest.TestCase):
    def setUp(self):
        from launcher_service import Service
        from linux_support import LinuxLayout
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.layout = LinuxLayout(self.root)
        self.layout.app = ROOT
        self.layout.writable()
        (self.root / "fixture").mkdir()
        fake = fake_installation(self.root / "fixture")
        self.layout.installation.parent.mkdir(parents=True, exist_ok=True)
        fake.rename(self.layout.installation)
        self.service = Service(self.layout, lambda event: None, testing=True)
        self.wave = self.root / "tone.wav"
        self.wave.write_bytes(wave_bytes(frames=44100 * 2))

    def tearDown(self):
        self.tmp.cleanup()

    def wait_idle(self):
        import time
        deadline = time.monotonic() + 10
        while self.service.busy and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.service.busy)

    def test_soundtrack_commands_are_validated_and_drive_the_project(self):
        status = self.service.handle("soundtrack", {})
        self.assertEqual(len(status["slots"]), 10)
        self.assertEqual(status["exports"], [])
        for command, args in (("soundtrack_import", {"slot": 3}), ("soundtrack_import", {"slot": 3, "path": "x.mp3"}),
                              ("soundtrack_import", {"slot": "3", "path": str(self.wave)}),
                              ("soundtrack_move", {"slot": 3, "offset": 2}), ("soundtrack_details", {"details": "x"}),
                              ("soundtrack_preview", {"slot": "2"})):
            with self.subTest(command=command, args=args), self.assertRaises(ValueError):
                self.service.handle(command, args)
        revision = self.service.snapshot()["soundtrack_revision"]
        self.service.handle("soundtrack_import", {"slot": 3, "path": str(self.wave)})
        self.service.handle("soundtrack_move", {"slot": 3, "offset": -1})
        self.assertGreater(self.service.snapshot()["soundtrack_revision"], revision)
        rows = self.service.handle("soundtrack", {})["slots"]
        self.assertEqual((rows[0]["state"], rows[1]["state"]), ("replacement", "moved"))
        with patch.object(self.service, "play_preview") as play:
            self.service.handle("soundtrack_preview", {"slot": 2})
            self.service.handle("soundtrack_preview", {"slot": None})
        self.assertEqual(play.call_count, 2)
        self.assertTrue(all(Path(c.args[0]).suffix == ".wav" for c in play.call_args_list))
        self.service.busy, self.service.phase = True, "running"
        for command, args in (("soundtrack_restore", {"slot": 2}), ("soundtrack_reset", {}),
                              ("soundtrack_import", {"slot": 4, "path": str(self.wave)})):
            with self.subTest(command=command), self.assertRaisesRegex(ValueError, "Wait for the current task"):
                self.service.handle(command, args)
        self.service.busy, self.service.phase = False, "idle"
        self.service.handle("soundtrack_details", {"details": {"id": "tester.tones", "name": "Tones"}})
        self.service.handle("soundtrack_export", {"install": True})
        self.wait_idle()
        self.assertEqual(self.service.title, "Exported and installed Tones 1.0.0. Enable it in Mods.")
        self.assertEqual(self.service.handle("soundtrack", {})["exports"], ["tester.tones-1.0.0.psxmod"])
        [package] = self.service.handle("mods", {})["packages"]
        self.assertEqual((package["id"], package["required_plugins"]), ("tester.tones", ["nitrogen.soundtrack"]))
        with patch.object(self.service, "open_path") as opened:
            self.service.handle("open", {"target": "soundtrack-exports"})
        opened.assert_called_once_with(self.layout.local / "soundtrack/exports")


class PluginSourceTests(unittest.TestCase):
    def test_plugin_uses_only_the_cdda_playlist_api(self):
        source = (ROOT / "native/mods/soundtrack.cpp").read_text(encoding="utf-8")
        self.assertIn('plugin_id = "nitrogen.soundtrack"', source)
        self.assertIn("psx_mod_append_cdda_tracks(tracks.data(), slots, lbas, counts)", source)
        self.assertIn(f'"{PLAYLIST_HEADER}"', source)
        self.assertIn("first_slot = 2, last_slot = 11", source)
        for forbidden in ("psx_mod_write", "psx_mod_host_write", "func_override_add", "g_psx_ram", "fopen"):
            self.assertNotIn(forbidden, source)

    def test_player_builds_link_the_soundtrack_plugin_and_probe_stays_optional(self):
        module = (ROOT / "cmake/nitrogen_mods.cmake").read_text(encoding="utf-8")
        self.assertIn('_nitrogen_player_mod_sources\n    "${CMAKE_CURRENT_SOURCE_DIR}/native/mods/soundtrack.cpp")', module)
        self.assertIn('if("${PSXRECOMP_ROOT}/runtime/src/mod_runtime.cpp" IN_LIST _nitrogen_sources)\n'
                      "    target_sources(nitrogen-runtime PRIVATE ${_nitrogen_player_mod_sources})\n"
                      "    if(NITROGEN_MODS)\n        target_sources(nitrogen-runtime PRIVATE ${_nitrogen_development_mod_sources})",
                      module)


if __name__ == "__main__":
    unittest.main()
