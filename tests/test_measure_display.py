"""Display measurement tooling: synthetic data only, no game files or GPU."""
import hashlib
import json
from pathlib import Path
import re
import struct
import sys
import tempfile
import unittest
import unittest.mock
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "measurement"))
sys.path.insert(0, str(ROOT / "tools"))
import display_report as report  # noqa: E402
import measure_display as measure  # noqa: E402
import windows_displays  # noqa: E402

PERIOD_NS = int(1e9 / report.GUEST_HZ)


def frame_rows(count, start=0, anchor=0, wall_ns=PERIOD_NS, busy_ns=5_000_000, flip=True, page=4,
               buttons=report.CROSS, slow=None):
    rows, t, busy = [], 0, 0
    for i in range(count):
        frame = start + i
        step = wall_ns if not slow or frame not in slow else slow[frame][0]
        t += step
        busy += busy_ns if not slow or frame not in slow else slow[frame][1]
        rows.append(dict(frame=frame, t_ns=t, busy=busy, rth_frames=i, rth_cpu_ns=i * 2_000_000,
                         rth_gpu_ns=i * 3_000_000, rth_gpu_frames=i, display_x=0 if (i % 2 == 0 or not flip) else 512,
                         display_y=0, width=364, height=240, depth24=0, disabled=0, instrumented=0,
                         page=page, next_page=page, old_page=page, buttons=buttons))
    return rows


class RouteTests(unittest.TestCase):
    def test_routes_wait_for_screens_and_count_gameplay_from_level_start(self):
        default = report.route_config("default")
        kinds = [(s["kind"], s["a"], s["repeat"]) for s in default["stages"]]
        self.assertEqual(kinds, [(0, report.TITLE_WIDTH, 0), (0, report.MENU_WIDTH, 0), (1, report.MAIN_MENU, 1),
                                 (1, report.SHIP_SELECT, 1), (1, report.LEVEL_SELECT, 0), (2, 0, 0)])
        self.assertEqual(sum(f for _, f in report.START_STEPS), 330)
        self.assertEqual(sum(f for _, f in report.GAMEPLAY_STEPS), 3100)
        play = default["stages"][-1]["steps"]
        pause_at = sum(f for _, f in play[:play.index((report.START, 20))])
        self.assertEqual(pause_at, dict((n, s) for n, s, _ in report.SCENES)["pause"])
        long = report.route_config("long")
        self.assertGreater(long["stop_rel"], sum(f for _, f in long["stages"][-1]["steps"]) - 100)
        self.assertTrue(all(c < default["stop_rel"] for c in default["captures_rel"]))
        with self.assertRaises(ValueError):
            report.route_config("unknown")

    def test_config_lines_carry_stages_steps_and_relative_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / ".local" / "run"
            run_dir.mkdir(parents=True)
            text = measure.write_config(dict(id="x", route="default", mode="windowed", width=1920, height=1080,
                                             golden=True), run_dir, 0).read_text()
            stage_lines = [l for l in text.splitlines() if l.startswith("stage ")]
            self.assertEqual(stage_lines[2], f"stage 1 {report.MAIN_MENU} 0 30 {report.WAIT_LIMIT} 1")
            self.assertIn("stop_rel 3450", text)
            self.assertIn("guest_capture 1", text)
            self.assertEqual(len([l for l in text.splitlines() if l.startswith("capture_rel ")]),
                             len(report.DEFAULT_CAPTURES))
            probe = measure.write_config(dict(id="p", probe=True, mode="none"), run_dir, None).read_text()
            self.assertIn("stop 400", probe)
            self.assertNotIn("stage ", probe)


class SceneAndFrameTests(unittest.TestCase):
    def test_labels_follow_schedule_pages_and_level_two_entry(self):
        rows = frame_rows(40, start=0) + frame_rows(5000, start=100)
        for r in rows[:10]:
            r["depth24"] = 1
        bonus = 100 + 3600
        for r in rows:
            if r["frame"] >= bonus:
                r["page"] = r["next_page"] = 21
            if r["frame"] >= bonus + 100:
                r["page"] = r["next_page"] = 7
            r["buttons"] = report.NONE if bonus + 100 <= r["frame"] < bonus + 200 else report.CROSS
        labels = dict(zip((r["frame"] for r in rows), report.label_scenes(rows, 100)))
        self.assertEqual(labels[0], "movie")
        self.assertEqual(labels[20], "boot-title-loading-and-menus")
        self.assertEqual(labels[100], "level-1-loading-and-help")
        self.assertEqual(labels[100 + 1600], "pause")
        self.assertEqual(labels[bonus + 10], "level-1-bonus-tally")
        self.assertEqual(labels[bonus + 150], "next-level-select")
        self.assertEqual(labels[bonus + 250], "level-2-entry")
        self.assertEqual(labels[bonus + 200 + report.ENTRY_FRAMES + 1], "level-2-gameplay")

    def test_cadence_frame_times_and_hitch_attribution(self):
        slow = {500: (40_000_000, 38_000_000), 600: (45_000_000, 2_000_000)}
        rows = frame_rows(1200, start=0, anchor=0, slow=slow)
        rows[700]["instrumented"] = 1
        timing = dict(busy_unit="thread_cpu_ns", measure_anchor=1)
        result = report.frame_analysis(rows, timing)
        gameplay = result["scenes"]["level-1-gameplay"]
        self.assertAlmostEqual(gameplay["game_frame_rate"], report.GUEST_HZ, delta=0.5)
        # Every flip is one VBlank after the previous one (the first counts from the prior scene).
        self.assertEqual(gameplay["vblanks_between_flips"], {"1": gameplay["display_flips"]})
        self.assertAlmostEqual(gameplay["render_thread_gpu_span_ms"]["p50"], 3.0)
        self.assertIn("swap", result["gpu_timer_note"])
        causes = {h["frame"]: h["attribution"] for h in result["hitches"]}
        self.assertTrue(causes[500].startswith("emulation CPU"))
        self.assertTrue(causes[600].startswith("waiting"))
        self.assertEqual(result["scenes"]["level-1-loading-and-help"]["instrumented_intervals_excluded"], 0)
        self.assertEqual(sum(s["instrumented_intervals_excluded"] for s in result["scenes"].values()), 2)

    def test_missing_gpu_timer_results_are_reported_unavailable_not_zero(self):
        rows = frame_rows(200)
        for r in rows:
            r["rth_gpu_frames"] = r["rth_gpu_ns"] = 0
        result = report.frame_analysis(rows, dict(busy_unit="thread_cycles", tsc_start=0, tsc_end=0))
        scene = result["scenes"]["boot-title-loading-and-menus"]
        self.assertTrue(str(scene["render_thread_gpu_span_ms"]).startswith("unavailable"))
        self.assertTrue(str(scene["emulation_thread_busy_ms"]).startswith("unavailable"))

    def test_gpu_timer_span_never_attributes_a_hitch_to_the_gpu(self):
        # Long GPU span and an idle game thread: reported as waiting, not GPU.
        self.assertTrue(report.attribute(40.0, 10.0, 2.0).startswith("waiting"))
        self.assertTrue(report.attribute(40.0, 36.0, 2.0).startswith("emulation CPU"))
        self.assertTrue(report.attribute(40.0, 25.0, 20.0).startswith("render-thread CPU"))
        rows = frame_rows(300, slow={250: (60_000_000, 5_000_000)})
        rows[250]["rth_gpu_ns"] += 50_000_000
        result = report.frame_analysis(rows, dict(busy_unit="thread_cpu_ns"))
        self.assertFalse(any("GPU (" in h["attribution"] for h in result["hitches"]))
        self.assertIn("render_gpu_span_ms", result["hitches"][0])

    def test_native_movie_and_present_capture_aftermath_are_not_hitches(self):
        movie = {100: (8_000_000_000, 2_000_000_000)}          # 8 s inside one guest frame
        rows = frame_rows(400, slow={**movie, 300: (66_000_000, 9_000_000)})
        swaps = [dict(before_ns=0, after_ns=0, guest_frame=f, ok=1, instrumented=0) for f in range(400)]
        swaps += [dict(before_ns=0, after_ns=0, guest_frame=99, ok=1, instrumented=0)] * 1000
        swaps.append(dict(before_ns=0, after_ns=0, guest_frame=298, ok=1, instrumented=1))
        swaps.sort(key=lambda s: s["guest_frame"])
        result = report.frame_analysis(rows, dict(busy_unit="thread_cpu_ns"), swaps)
        self.assertEqual(result["hitches"], [])
        self.assertIn("native-movie", result["scenes"])
        self.assertNotIn("wall_ms_per_guest_frame", result["scenes"]["native-movie"])
        settled = sum(s["after_present_capture_excluded"] for s in result["scenes"].values())
        self.assertEqual(settled, report.CAPTURE_SETTLE_FRAMES + 1)
        # Without the swap log both still count, as before.
        self.assertEqual(len(report.frame_analysis(rows, dict(busy_unit="thread_cpu_ns"))["hitches"]), 2)

    def test_windows_thread_cycles_use_the_measured_tsc_rate(self):
        self.assertEqual(report.busy_scale(dict(busy_unit="thread_cycles", start_ns=0, end_ns=2_000_000_000,
                                                tsc_start=0, tsc_end=6_000_000_000)), 1 / 3)
        self.assertIsNone(report.busy_scale(dict(busy_unit="thread_cycles")))


class PresentationTests(unittest.TestCase):
    def test_59_94_on_144_hz_alternates_two_and_three_refreshes(self):
        refresh = 144.0
        period = 1e9 / refresh
        swaps, t, owed = [], 0.0, 0.0
        for frame in range(600):
            owed += refresh / report.GUEST_HZ
            n = int(owed)
            owed -= n
            t += n * period
            swaps.append(dict(before_ns=int(t - 1e6), after_ns=int(t), guest_frame=frame, ok=1, instrumented=0))
        result = report.presentation_analysis(swaps, refresh, 1)
        self.assertEqual(set(result["intervals_in_refresh_periods"]), {"2", "3"})
        self.assertAlmostEqual(result["expected_refreshes_per_guest_frame"], 2.402, places=3)
        self.assertGreater(result["off_guest_period_over_2ms"], 0.9)
        self.assertEqual(result["repeated_guest_frame_presents"], 0)

    def test_even_pacing_repeats_and_unavailable_presents(self):
        swaps = [dict(before_ns=i * PERIOD_NS, after_ns=i * PERIOD_NS + 100, guest_frame=i // 2 * 2, ok=1,
                      instrumented=0) for i in range(100)]
        result = report.presentation_analysis(swaps, None, 1)
        self.assertEqual(result["repeated_guest_frame_presents"], 50)
        self.assertEqual(result["hitches_over_25ms"], 0)
        self.assertNotIn("intervals_in_refresh_periods", result)
        self.assertFalse(report.presentation_analysis([], 60.0, 0)["available"])


    def test_movie_presents_are_reported_apart_from_game_presents(self):
        refresh = 144.0
        movie_period, game_period = 1e9 / refresh, PERIOD_NS
        swaps, t = [], 0.0
        for i in range(300):                                     # 144 Hz movie inside guest frame 10
            t += movie_period
            swaps.append(dict(before_ns=int(t), after_ns=int(t), guest_frame=10, ok=1, instrumented=0))
        for frame in range(11, 611):                             # evenly paced game frames
            t += game_period
            swaps.append(dict(before_ns=int(t), after_ns=int(t), guest_frame=frame, ok=1, instrumented=0))
        scenes = {f: ("level-1-gameplay" if f >= 11 else "native-movie") for f in range(611)}
        result = report.presentation_analysis(swaps, refresh, 1, scenes)
        self.assertEqual(result["native_movie"]["intervals_in_refresh_periods"], {"1": 299})
        self.assertEqual(set(result["intervals_in_refresh_periods"]), {"2"})
        self.assertAlmostEqual(result["interval_ms"]["p50"], game_period / 1e6, places=2)
        self.assertEqual(result["by_scene"]["level-1-gameplay"]["hitches_over_25ms"], 0)
        self.assertEqual(result["repeated_guest_frame_presents"], 0)


class MemoryTests(unittest.TestCase):
    def test_hr_surface_formula_matches_the_estimate_for_5120x1440(self):
        scale = dict(fbo=[1024 * 15, 512 * 15], alloc_scale=15, effective=15, windowed=0)
        result = report.nominal_memory(scale, None)
        hr = result["items"][0]
        self.assertEqual(hr["bytes"], 943_718_400)          # 15360 x 7680 x (4 + 4)
        self.assertEqual((hr["width"], hr["height"], hr["buffers"]), (15360, 7680, 2))
        self.assertEqual(result["items"][1]["width"], report.SCRATCH_TILE)

    def test_world_raster_above_the_texture_limit_is_not_counted(self):
        result = report.nominal_memory(None, (28807, 2880), 16384)
        self.assertEqual(result["items"][0]["bytes"], 0)
        self.assertIn("exceeds GL_MAX_TEXTURE_SIZE", result["items"][0]["formula"])
        fits = report.nominal_memory(None, (14404, 1440), 16384)
        self.assertEqual(fits["total_bytes"], 14404 * 1440 * 8)

    def test_driver_memory_reporting(self):
        samples = [dict(source="NVX", dedicated_kib=11 << 20, total_kib=11 << 20, available_kib=10 << 20,
                        evictions=0, evicted_kib=0),
                   dict(source="NVX", dedicated_kib=11 << 20, total_kib=11 << 20, available_kib=9 << 20,
                        evictions=1, evicted_kib=0)]
        result = report.measured_memory(samples)
        self.assertEqual((result["used_mib_first"], result["used_mib_peak"]), (1024.0, 2048.0))
        self.assertFalse(report.measured_memory([])["available"])


class ImageTests(unittest.TestCase):
    def test_raw_capture_to_png_round_trip_and_bottom_up_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "present-1.raw"
            pixels = bytes(range(2 * 3 * 4))
            raw.write_bytes(struct.pack("<4I", 0x5741524E, 2, 3, 4) + pixels)
            w, h, c, data = report.read_raw(raw)
            digest = report.write_png(Path(tmp) / "x.png", w, h, c, data, bottom_up=True)
            png = (Path(tmp) / "x.png").read_bytes()
            self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
            idat = png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8]
            rows = zlib.decompress(idat)
            top_down = pixels[16:24] + pixels[8:16] + pixels[0:8]
            self.assertEqual(rows, b"".join(b"\x00" + top_down[i:i + 8] for i in range(0, 24, 8)))
            self.assertEqual(digest, hashlib.sha256(top_down).hexdigest())
            raw.write_bytes(b"nope" * 8)
            with self.assertRaises((ValueError, struct.error)):
                report.read_raw(raw)

    def test_golden_comparison(self):
        same = report.compare_goldens({"a.png": "1", "b.png": "2"}, {"a.png": "1", "b.png": "2"})
        self.assertTrue(same["deterministic"])
        diff = report.compare_goldens({"a.png": "1"}, {"a.png": "2", "b.png": "3"})
        self.assertEqual((diff["deterministic"], diff["differing"]), (False, ["a.png", "b.png"]))


OWNER_STYLE_DISPLAY = dict(
    index=0, name="Ultrawide", bounds=[0, 0, 3840, 1080], desktop=dict(w=3840, h=1080, refresh=144.0),
    modes=[dict(w=7680, h=2160, refresh=60.0), dict(w=5120, h=1440, refresh=59.94),
           dict(w=3840, h=1080, refresh=144.0), dict(w=3840, h=1080, refresh=119.98),
           dict(w=3840, h=1080, refresh=60.0), dict(w=1920, h=1080, refresh=144.0)])


class PlanTests(unittest.TestCase):
    def test_full_plan_separates_windowed_oversized_and_listed_display_modes(self):
        runs = {r["id"]: r for r in report.plan_runs(OWNER_STYLE_DISPLAY, "full")}
        self.assertEqual(runs["win-1920x1080-100pct-vsync1-golden-a"]["captures"], report.DEFAULT_CAPTURES)
        self.assertEqual(runs["win-3840x1080-200pct-vsync1"]["kind"], "windowed")
        self.assertEqual(runs["win-5120x1440-100pct-vsync1"]["kind"], "windowed-oversized")
        self.assertEqual(runs["win-7680x2160-200pct-vsync1"]["kind"], "windowed-oversized")
        for rate in (144, 119.98, 60):
            for vsync in (0, 1):
                self.assertIn(f"excl-3840x1080@{rate:g}-vsync{vsync}", runs)
        self.assertIn("excl-5120x1440@59.94-vsync1", runs)
        self.assertIn("excl-7680x2160@60-vsync1", runs)
        self.assertNotIn("excl-2560x1440@60-vsync1", runs)                # not listed by the display
        self.assertEqual(runs["excl-3840x1080@144-vsync1-long-a"]["captures"], None)
        self.assertEqual(runs["borderless-desktop-vsync1"]["video_mode"], 1)
        self.assertEqual(runs["fullscreen-desktop-vsync1"]["video_mode"], 2)
        self.assertGreater(report.estimate_minutes(list(runs.values())), 60)

    def test_quick_stutter_and_golden_presets(self):
        quick = [r["id"] for r in report.plan_runs(OWNER_STYLE_DISPLAY, "quick")]
        self.assertIn("excl-3840x1080@144-vsync0", quick)
        self.assertNotIn("excl-3840x1080@60-vsync1", quick)
        stutter = [r["id"] for r in report.plan_runs(OWNER_STYLE_DISPLAY, "stutter")]
        self.assertEqual(sum(1 for r in stutter if "long" in r), 3)
        small = dict(OWNER_STYLE_DISPLAY, bounds=[0, 0, 1280, 720], modes=[])
        golden = report.plan_runs(small, "golden")
        self.assertEqual([(r["width"], r["height"]) for r in golden], [(1280, 720)] * 2)
        self.assertEqual(report.pick_display([OWNER_STYLE_DISPLAY, dict(small, index=1)], None)["index"], 0)

    def test_v1_preset_uses_the_player_settings_and_keeps_v0_goldens(self):
        runs = {r["id"]: r for r in report.plan_runs(OWNER_STYLE_DISPLAY, "v1")}
        golden = runs["win-1920x1080-100pct-vsync1-golden-a"]
        self.assertEqual(golden["captures"], report.DEFAULT_CAPTURES)
        self.assertNotIn("aspect_option", golden)              # Auto, exactly as V0
        self.assertEqual(golden.get("vsync_via", "env"), "env")
        for option, label in report.ASPECTS:
            run = runs[f"v1-aspect-{label}"]
            self.assertEqual((run["aspect_option"], run["vsync_via"], run["video_mode"]), (option, "option", 1))
        self.assertEqual((runs["v1-vsync-off"]["vsync"], runs["v1-vsync-off"]["vsync_via"]), (0, "option"))
        self.assertEqual((runs["v1-hud-centred"]["hud_layout"], runs["v1-hud-centred"]["aspect_option"]), (1, 0))
        clamp = runs["v1-clamp-7680x2160-200pct"]
        self.assertEqual((clamp["width"], clamp["render_percent"], clamp["kind"]), (7680, 200, "windowed-oversized"))

    def test_options_file_and_vsync_override_per_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / ".local"
            out = local / "measurements" / "x"
            out.mkdir(parents=True)
            seen = {}

            def fake_run(command, **kwargs):     # record the game's environment; start nothing
                seen["env"] = kwargs.get("env")
                raise measure.subprocess.TimeoutExpired(command, 0)
            info = dict(work=local, disc_bin=Path(tmp) / "disc.bin", game=Path(tmp) / "game.toml")
            original = measure.subprocess.run
            measure.subprocess.run = fake_run
            try:
                with unittest.mock.patch.dict(measure.os.environ, PSX_VSYNC="1"):
                    # A V1 run uses the player's setting: the developer override is removed.
                    for run, expect in ((dict(id="a", aspect_option=3, vsync=0, vsync_via="option", hud_layout=1), None),
                                        (dict(id="b", vsync=0), "0")):
                        measure.run_once(Path(tmp) / "missing.exe", info, dict(run, kind="test", route="default", mode="none"),
                                         out, None, 1, False, False)
                        self.assertEqual(seen["env"].get("PSX_VSYNC"), expect)
                        options = (out / "runs" / run["id"] / "nitrogen-options.ini").read_text()
                        self.assertIn(f"aspect {run.get('aspect_option', 0)}\n", options)
                        self.assertIn(f"vsync {run['vsync']}\n", options)
                        self.assertIn(f"hud_layout {run.get('hud_layout', 0)}\n", options)
            finally:
                measure.subprocess.run = original

    def test_baseline_comparison_is_capture_exact(self):
        name = report.GOLDEN_RUN
        old = dict(created="v0", build=dict(build_identity=dict(inputs_sha256="4dc2")),
                   runs=[dict(id=name, status="completed", captures={"guest-0.png": "a", "present-0.png": "b"})])
        same = [dict(id=name, status="completed", captures={"guest-0.png": "a", "present-0.png": "b"})]
        moved = [dict(id=name, status="completed", captures={"guest-0.png": "a", "present-0.png": "c"})]
        self.assertTrue(report.compare_baseline(old, same)["identical_to_baseline"])
        result = report.compare_baseline(old, moved)
        self.assertFalse(result["identical_to_baseline"])
        self.assertEqual(result["differing"], ["present-0.png"])
        self.assertEqual(result["baseline_build"], "4dc2")
        self.assertIsNone(report.compare_baseline(old, [])["identical_to_baseline"])


class LinkTests(unittest.TestCase):
    def test_link_command_extraction_for_windows_ninja_and_posix(self):
        windows = ('C:\\Windows\\system32\\cmd.exe /C "cd . && "C:\\tools\\clang++.exe" -O3 @CMakeFiles\\r.rsp '
                   '-o NitrogenSquared-dev.exe -Wl,--out-implib,lib.dll.a && C:\\Windows\\system32\\cmd.exe /C "copy x""')
        posix = ": && /usr/bin/c++ -O3 CMakeFiles/a.o -o NitrogenSquared-dev -lSDL3 && :"
        if sys.platform == "win32":
            parts = measure.split_link(windows)
            self.assertEqual(parts[0], "C:\\tools\\clang++.exe")
            self.assertIn("@CMakeFiles\\r.rsp", parts)
        else:
            parts = measure.split_link(posix)
            self.assertEqual(parts[:2], ["/usr/bin/c++", "-O3"])
        self.assertEqual(parts[parts.index("-o") + 1].split("\\")[-1].split("/")[-1][:17], "NitrogenSquared-d")
        with self.assertRaises(measure.MeasureError):
            measure.split_link("c++ -Wl,--wrap=SDL_PollEvent a.o -o x")
        with self.assertRaises(measure.MeasureError):
            measure.split_link("c++ a.o")

    def test_ninja_response_file_is_rebuilt_with_escapes_and_continuations(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = Path(tmp)
            (build / "CMakeFiles").mkdir()
            (build / "CMakeFiles/rules.ninja").write_text(
                "rule LINK_X\n  command = c++ @$RSP_FILE -o $TARGET_FILE\n"
                "  rspfile = $RSP_FILE\n  rspfile_content = $in $LINK_PATH $LINK_LIBRARIES\n")
            (build / "build.ninja").write_text(
                "include CMakeFiles/rules.ninja\n"
                "build My$ Game.exe: LINK_X a.o dir$ x/b.o $\n    c.o | dep.txt || order\n"
                "  LINK_LIBRARIES = libSDL3.a  -lopengl32\n  RSP_FILE = CMakeFiles\\x.rsp\n\n")
            name, content = measure.ninja_response_file(build, "My Game.exe")
            self.assertEqual(name, "CMakeFiles\\x.rsp")
            self.assertEqual(content, 'a.o "dir x/b.o" c.o libSDL3.a  -lopengl32\n')
            self.assertIsNone(measure.ninja_response_file(build, "Other.exe"))

    def test_output_must_stay_private(self):
        with self.assertRaises(measure.MeasureError):
            measure.private(Path(tempfile.gettempdir()) / "public")
        self.assertIn(".local", measure.private(ROOT / ".local" / "measurements" / "x").parts)


class FixtureSourceTests(unittest.TestCase):
    def test_fixture_only_observes_guest_memory_and_wraps_three_sdl_calls(self):
        source = (ROOT / "tools/measurement/display_fixture.cpp").read_text(encoding="utf-8")
        self.assertEqual(sorted(set(re.findall(r"__wrap_(\w+)\(", source))),
                         sorted(measure.WRAPS))
        self.assertIn("psx_mod_set_controller_source(0, route_source)", source)
        # Guest RAM is only copied from, never written.
        self.assertEqual(re.findall(r"g_psx_ram[^;]*;", source).count("g_psx_ram;"), 1)
        self.assertNotRegex(source, r"g_psx_ram\s*\[[^\]]*\]\s*=")
        self.assertNotRegex(source, r"memcpy\(\s*g_psx_ram")
        self.assertNotRegex(source, r"write_(word|half|byte)\(")
        self.assertIn('part == ".local"', source)

    def test_windows_display_inventory_reports_availability(self):
        result = windows_displays.inventory()
        self.assertIn("available", result)
        if not result["available"]:
            self.assertTrue(result["reason"])
        else:
            for display in result["displays"]:
                self.assertIn("modes", display)


if __name__ == "__main__":
    unittest.main()
