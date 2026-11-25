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
        for s in found:
            self.assertEqual(s.get("options"), {}, "baseline scenarios use the original look")

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
