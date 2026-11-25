"""Scenario runner logic: validation, region comparison, tolerances and evidence.

Original synthetic images only; no game data, renderer or window is used.
"""
from pathlib import Path
import json
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "measurement"))
import n2test  # noqa: E402


def image(w, h, fill=(10, 20, 30), ch=3):
    px = bytearray()
    for _ in range(w * h):
        px += bytes(fill) + (b"\xff" if ch == 4 else b"")
    return [w, h, ch, px]


def paint(img, x, y, rgb):
    w, _, ch, px = img
    i = (y * w + x) * ch
    px[i:i + 3] = bytes(rgb)


def frozen(img):
    return (img[0], img[1], img[2], bytes(img[3]))


def scenario(**over):
    s = dict(id="t1", renderer="mesa", window=dict(mode="windowed", width=64, height=48), route="default",
             captures=[400], checks=[dict(type="image", capture=400, regions=[dict(name="all", rect=[0, 0, 1, 1])])])
    s.update(over)
    return s


class ValidationTests(unittest.TestCase):
    def test_shipped_scenarios_are_valid(self):
        found = n2test.load_scenarios([ROOT / "tools" / "measurement" / "scenarios"], [])
        self.assertGreaterEqual(len(found), 5)
        self.assertEqual({s["renderer"] for s in found}, {"software", "mesa"})
        originals = [s for s in found if s["id"].startswith("original-")]
        self.assertGreaterEqual(len(originals), 5)
        for s in originals:
            self.assertEqual(s.get("options"), {}, "original scenarios use the original look")
            self.assertEqual(s.get("baseline_set"), "original")
        for s in found:
            if s["id"].startswith("v4-"):
                self.assertEqual(s.get("baseline_set"), "v4", "feature baselines never replace the original set")

    def test_rejects_bad_scenarios(self):
        self.assertEqual(n2test.validate(scenario()), [])
        cases = [dict(renderer="vulkan"), dict(id="bad id!"), dict(route="campaign"),
                 dict(options=dict(bloom=2)), dict(renderer="software"),     # software has no presented image
                 dict(checks=[dict(type="image", capture=999)]),
                 dict(checks=[dict(type="image", capture=400, regions=[dict(rect=[0.5, 0, 0.4, 1])])]),
                 dict(checks=[dict(type="shader_compiles")])]
        for over in cases:
            with self.subTest(over=over):
                self.assertTrue(n2test.validate(scenario(**over)))


class ComparisonTests(unittest.TestCase):
    def test_identical_images_pass_exactly(self):
        a = image(64, 48)
        stats = n2test.compare_region(frozen(a), frozen(a), [0, 0, 1, 1])
        self.assertEqual((stats["pixels"], stats["changed"], stats["max_abs"]), (64 * 48, 0, 0))
        self.assertTrue(n2test.within(stats, {"exact": True}))

    def test_one_pixel_fails_exact_and_regions_localise_it(self):
        a, b = image(64, 48), image(64, 48)
        paint(b, 60, 2, (10, 20, 40))                      # top-right corner, +10 blue
        whole = n2test.compare_region(frozen(a), frozen(b), [0, 0, 1, 1])
        self.assertEqual((whole["changed"], whole["max_abs"]), (1, 10))
        self.assertFalse(n2test.within(whole, {"exact": True}))
        left = n2test.compare_region(frozen(a), frozen(b), [0, 0, 0.5, 1])
        self.assertEqual(left["changed"], 0)
        self.assertTrue(n2test.within(whole, {"max_changed_ratio": 0.001, "max_mean_abs": 0.01, "max_abs": 10}))
        self.assertFalse(n2test.within(whole, {"max_changed_ratio": 0.001, "max_mean_abs": 0.01, "max_abs": 9}))

    def test_ignore_masks_and_alpha(self):
        a, b = image(64, 48, ch=4), image(64, 48, ch=4)
        paint(b, 32, 24, (255, 255, 255))
        b[3][(24 * 64 + 10) * 4 + 3] = 0                   # alpha differs: not a colour change
        masked = n2test.compare_region(frozen(a), frozen(b), [0, 0, 1, 1], [[0.45, 0.45, 0.55, 0.55]])
        self.assertEqual(masked["changed"], 0)
        self.assertLess(masked["pixels"], 64 * 48)
        unmasked = n2test.compare_region(frozen(a), frozen(b), [0, 0, 1, 1])
        self.assertEqual(unmasked["changed"], 1)

    def test_size_mismatch_always_fails(self):
        stats = n2test.compare_region(frozen(image(64, 48)), frozen(image(32, 48)), [0, 0, 1, 1])
        self.assertTrue(stats["size_mismatch"])
        self.assertFalse(n2test.within(stats, {"max_changed_ratio": 1, "max_mean_abs": 255, "max_abs": 255}))

    def test_present_captures_are_flipped_to_top_down(self):
        with tempfile.TemporaryDirectory() as temp:
            raw = Path(temp) / "present-1.raw"
            rows = [bytes([y] * 6) for y in range(3)]       # 2x3 RGB, row value = y
            raw.write_bytes(struct.pack("<4I", 0x5741524E, 2, 3, 3) + b"".join(rows))
            self.assertEqual(n2test.read_image(raw, True)[3][:6], rows[2])
            self.assertEqual(n2test.read_image(raw, False)[3][:6], rows[0])


class MaskTests(unittest.TestCase):
    """UI masks come from a frame drawn with and without Nitrogen's HUD/menu primitives."""

    def frames(self):
        world = image(64, 48, (40, 90, 40))
        with_ui = image(64, 48, (40, 90, 40))
        for x in range(4, 20):                 # an opaque HUD bar, rows 2-5
            for y in range(2, 6):
                paint(with_ui, x, y, (250, 250, 250))
        return frozen(with_ui), frozen(world)

    def test_mask_marks_ui_pixels_and_grows_their_edges(self):
        with_ui, without = self.frames()
        core = n2test.make_mask(with_ui, without, grow=0)
        self.assertEqual(core.count(1), 16 * 4)
        grown = n2test.make_mask(with_ui, without, grow=1)
        self.assertEqual(grown.count(1), 18 * 6)
        self.assertEqual(grown[1 * 64 + 3], 1)          # diagonal neighbour included
        self.assertEqual(grown[0 * 64 + 3], 0)

    def test_mask_round_trip_and_rejects_corruption(self):
        with_ui, without = self.frames()
        mask = n2test.make_mask(with_ui, without)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "a" / "present-400.uimask"
            n2test.save_mask(path, 64, 48, mask)
            self.assertEqual(n2test.load_mask(path), (64, 48, mask))
            path.write_bytes(b"XXXX" + path.read_bytes()[4:])
            with self.assertRaises(ValueError):
                n2test.load_mask(path)

    def test_ui_and_world_regions_are_judged_separately(self):
        with_ui, without = self.frames()
        mask = n2test.make_mask(with_ui, without)
        smoothed = image(64, 48, (40, 90, 40))
        for x in range(4, 20):
            for y in range(2, 6):
                paint(smoothed, x, y, (250, 250, 250))
        for x in range(30, 60):                 # the world changes (smooth filtering)
            paint(smoothed, x, 40, (44, 94, 44))
        smoothed = frozen(smoothed)
        ui = n2test.compare_region(smoothed, with_ui, [0, 0, 1, 1], mask=mask, want=1)
        world = n2test.compare_region(smoothed, with_ui, [0, 0, 1, 1], mask=mask, want=0)
        self.assertEqual((ui["pixels"], ui["changed"]), (mask.count(1), 0))
        self.assertEqual((world["pixels"], world["changed"]), (64 * 48 - mask.count(1), 30))
        self.assertTrue(n2test.within(ui, {"exact": True}))
        self.assertFalse(n2test.within(world, {"exact": True}))
        bad = n2test.compare_region(smoothed, with_ui, [0, 0, 1, 1], mask=mask[:-1], want=1)
        self.assertTrue(bad["size_mismatch"])

    def test_mask_regions_validate(self):
        good = scenario(checks=[dict(type="image", capture=400, regions=[dict(name="hud", mask="ui"),
                                                                         dict(name="world", mask="world")])])
        self.assertEqual(n2test.validate(good), [])
        bad = scenario(checks=[dict(type="image", capture=400, regions=[dict(name="x", mask="hud")])])
        self.assertTrue(n2test.validate(bad))

    def test_missing_mask_is_not_tested_not_passed(self):
        result = dict(id="s", renderer="mesa", window=[64, 48], seconds=1.0, status="completed", passed=False,
                      gl="", evidence=["not verified"],
                      checks=[dict(type="image", capture=400, passed=None,
                                   regions=[dict(name="hud", passed=None, pixels=0, changed=0, changed_ratio=0.0,
                                                 mean_abs=0.0, max_abs=0, size_mismatch=False,
                                                 detail="not tested: no UI mask (run --make-masks)")])])
        text = n2test.markdown([result], "now")
        self.assertIn("image 400 [hud]: not tested: no UI mask", text)
        self.assertIn("| not tested |", text)


def write_present(path, img):
    """Write a top-down test image as a bottom-up present capture."""
    w, h, ch, px = img
    rows = [bytes(px[y * w * ch:(y + 1) * w * ch]) for y in range(h)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<4I", 0x5741524E, w, h, ch) + b"".join(reversed(rows)))


class HudSharpTests(unittest.TestCase):
    """hud_sharp: the HUD's own layer and its opaque pixels are exact; translucent
    and ambiguous HUD pixels may change only because the world behind them changed."""

    def build(self, world=(40, 90, 40), hud_layer_pixel=(250, 250, 250), dark=(0, 0, 0)):
        composite, layer, behind = image(64, 48, world), image(64, 48, (0, 0, 0)), image(64, 48, world)
        for x in range(4, 20):                  # opaque HUD bar
            for y in range(2, 6):
                paint(composite, x, y, (250, 250, 250))
                paint(layer, x, y, hud_layer_pixel if (x, y) == (10, 3) else (250, 250, 250))
        for x in range(30, 34):                 # translucent HUD panel: half world, half HUD
            paint(layer, x, 10, (60, 60, 60))
            paint(composite, x, 10, tuple(min(255, c // 2 + 60) for c in world))
        paint(behind, 3, 3, (0, 0, 0))          # black world beside the bar, inside the grown mask:
        paint(composite, 3, 3, dark)            # black HUD over black world proves nothing
        return composite, layer, behind

    def run_check(self, now_composite, now_layer):
        ref_composite, ref_layer, behind = self.build()
        with tempfile.TemporaryDirectory() as temp:
            out, base = Path(temp) / "out", Path(temp) / "base"
            paths = n2test.hud_paths(base, "original", "mesa", "ref", 400)
            write_present(paths["ref"], ref_composite)
            write_present(paths["layer"], ref_layer)
            write_present(paths["world"], behind)
            mask = n2test.make_mask(frozen(ref_composite), frozen(behind), grow=1)
            n2test.save_mask(paths["mask"], 64, 48, mask)
            write_present(out / "runs" / "t1-hud-ref" / "present-400.raw", now_composite)
            write_present(out / "runs" / "t1-hud-layer" / "present-400.raw", now_layer)
            return n2test.hud_sharp(scenario(), dict(type="hud_sharp", capture=400, against="ref",
                                                     against_set="original"), out, base)

    def test_world_change_behind_translucent_hud_is_reported_not_failed(self):
        composite, layer, _ = self.build(world=(44, 96, 44))   # the world is smoothed
        result = self.run_check(composite, layer)
        self.assertIs(result["passed"], True)
        self.assertGreaterEqual(result["translucent"]["changed"], 4)
        self.assertIn("translucent HUD pixels", result["detail"])

    def test_world_change_behind_black_over_black_is_ambiguous_not_failed(self):
        composite, layer, _ = self.build(dark=(8, 8, 0))       # smoothing tints the black world
        result = self.run_check(composite, layer)
        self.assertIs(result["passed"], True)
        self.assertEqual(result["regions"][1]["changed"], 0)
        self.assertEqual((result["ambiguous"]["pixels"], result["ambiguous"]["changed"]), (1, 1))

    def test_a_changed_hud_layer_fails(self):
        composite, layer, _ = self.build(hud_layer_pixel=(240, 240, 240))  # the HUD texture itself changed
        result = self.run_check(composite, layer)
        self.assertIs(result["passed"], False)
        self.assertFalse(result["regions"][0]["passed"])

    def test_a_changed_opaque_hud_pixel_fails(self):
        composite, layer, _ = self.build()
        paint(composite, 12, 4, (200, 200, 200))
        result = self.run_check(composite, layer)
        self.assertIs(result["passed"], False)
        self.assertEqual(result["regions"][1]["changed"], 1)

    def test_classes_separate_opaque_translucent_and_ambiguous(self):
        composite, layer, behind = self.build()
        mask = n2test.make_mask(frozen(composite), frozen(behind), grow=0)
        classes = n2test.hud_classes(mask, frozen(composite), frozen(layer), frozen(behind))
        self.assertEqual([classes.count(k) for k in (n2test.OPAQUE, n2test.TRANSLUCENT, n2test.AMBIGUOUS)],
                         [16 * 4, 4, 0])
        grown = n2test.make_mask(frozen(composite), frozen(behind), grow=1)
        classes = n2test.hud_classes(grown, frozen(composite), frozen(layer), frozen(behind))
        self.assertEqual(classes[3 * 64 + 3], n2test.AMBIGUOUS)   # black over black
        self.assertEqual(classes.count(n2test.OPAQUE), 16 * 4)

    def test_missing_references_are_not_tested(self):
        with tempfile.TemporaryDirectory() as temp:
            result = n2test.hud_sharp(scenario(), dict(type="hud_sharp", capture=400, against="ref"),
                                      Path(temp), Path(temp))
        self.assertIsNone(result["passed"])
        self.assertIn("run --make-masks for ref", result["detail"])

    def test_hud_sharp_validates(self):
        self.assertEqual(n2test.validate(scenario(checks=[dict(type="hud_sharp", capture=400, against="a")])), [])
        self.assertTrue(n2test.validate(scenario(checks=[dict(type="hud_sharp", capture=401)])))


class ScenarioActionTests(unittest.TestCase):
    """Mid-run settings, presets, saved options, restarts and must-change checks."""

    def test_option_keys_follow_the_options_header(self):
        import re
        header = (ROOT / "compat" / "nitrogen_options.h").read_text(encoding="utf-8")
        body = header[header.index("enum { NITROGEN_VIDEO_MODE"):]
        body = body[:body.index("NITROGEN_OPTION_COUNT")]
        names = re.findall(r"NITROGEN_([A-Z_]+)", body)
        self.assertEqual(list(n2test.OPTION_KEYS), [n.lower() for n in names])
        game = (ROOT / "compat" / "game_options.cpp").read_text(encoding="utf-8")
        for key in n2test.OPTION_KEYS:
            self.assertIn(f'"{key}"', game)          # the saved file uses the same names

    def test_presets_follow_the_header(self):
        header = (ROOT / "compat" / "nitrogen_options.h").read_text(encoding="utf-8")
        order = ["ORIGINAL", "SMOOTH", "CRT", "COMPOSITE"]
        positions = [header.index(f"NITROGEN_PICTURE_{n}") for n in order]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(list(n2test.PICTURE_PRESETS), [n.lower() for n in order])

    def test_actions_and_restart_validate(self):
        good = scenario(set_options_at=[[300, "texture_filter", 1]], preset_at=[[300, "original"]],
                        restart=dict(checks=[dict(type="route_completed")]),
                        checks=[dict(type="options_saved", values=dict(texture_filter=1)),
                                dict(type="image", capture=400, against="original-mesa-1024x768",
                                     regions=[dict(name="hud", mask="ui")], tolerance=dict(exact=True))])
        self.assertEqual(n2test.validate(good), [])
        for over in [dict(set_options_at=[[300, "bloom", 1]]), dict(set_options_at=[[300, "texture_filter"]]),
                     dict(preset_at=[[300, "neon"]]), dict(restart=dict()),
                     dict(checks=[dict(type="options_saved", values=dict(bloom=1))]),
                     dict(restart=dict(checks=[dict(type="image", capture=999)]))]:
            with self.subTest(over=over):
                self.assertTrue(n2test.validate(scenario(**over)))

    def test_expect_change_fails_when_a_setting_does_nothing(self):
        a = image(64, 48)
        same = n2test.compare_region(frozen(a), frozen(a), [0, 0, 1, 1])
        self.assertFalse(n2test.within(same, {"expect_change": True}))
        b = image(64, 48)
        paint(b, 1, 1, (0, 0, 0))
        self.assertTrue(n2test.within(n2test.compare_region(frozen(a), frozen(b), [0, 0, 1, 1]),
                                      {"expect_change": True}))


class EvidenceTests(unittest.TestCase):
    def test_labels_never_overstate(self):
        mesa = "OpenGL context created (4.6 (Core Profile) Mesa 26.3.1; llvmpipe (LLVM 23.1.5, 256 bits))"
        nvidia = "OpenGL context created (3.3.0 NVIDIA 582.78; NVIDIA GeForce GTX 1080 Ti/PCIe/SSE2)"
        self.assertIn("software-rendered (Mesa llvmpipe; not GPU-representative)", n2test.evidence("mesa", mesa, True))
        self.assertIn("hardware-rendered", n2test.evidence("opengl", nvidia, True))
        self.assertNotIn("hardware-rendered", n2test.evidence("opengl", mesa, True))   # llvmpipe is never hardware
        self.assertNotIn("hardware-rendered", n2test.evidence("opengl", "GDI Generic", False))
        self.assertNotIn("gameplay-tested (scripted route)", n2test.evidence("mesa", mesa, False))
        self.assertTrue(any("software-rasterised" in e for e in n2test.evidence("software", "", True)))

    def test_mesa_manifest_requires_both_files(self):
        with tempfile.TemporaryDirectory() as temp:
            local = Path(temp)
            (local / "tools" / "mesa").mkdir(parents=True)
            manifest = local / "tools" / "mesa" / "MANIFEST.txt"
            manifest.write_text("header line\n" + "a" * 64 + " *x64/opengl32.dll\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                n2test.mesa_files(local)
            manifest.write_text(manifest.read_text() + "b" * 64 + " *x64/libgallium_wgl.dll\n", encoding="utf-8")
            self.assertEqual(n2test.mesa_files(local)["libgallium_wgl.dll"], "b" * 64)

    def test_report_marks_missing_baselines_as_not_tested(self):
        result = dict(id="s", renderer="mesa", window=[64, 48], seconds=1.0, status="completed", passed=False,
                      gl="", evidence=["not verified"],
                      checks=[dict(type="image", capture=400, passed=None, detail="not tested: no baseline")])
        text = n2test.markdown([result], "now")
        self.assertIn("| s | mesa | 64x48 | not tested |", text)
        self.assertIn("image 400: not tested", text)
        recorded = dict(result, recorded=True, evidence=["baseline recorded; not verified"],
                        checks=[dict(type="image", capture=400, passed=None, detail="baseline updated")])
        self.assertIn("| s | mesa | 64x48 | baseline recorded |", n2test.markdown([recorded], "now"))
        failed = dict(result, checks=[dict(type="route_completed", passed=False, detail="timeout")])
        self.assertIn("| FAIL |", n2test.markdown([failed], "now"))


if __name__ == "__main__":
    unittest.main()
