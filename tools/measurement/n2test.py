"""Nitrogen scenario tests: run real-game scenarios and compare them with baselines.

Each scenario (JSON, original text) names a window, Nitrogen options, a
controller route from tools/measurement and the frames to capture, plus checks:

  route_completed   the route reached its last frame with every awaited screen
  page_at           the game's menu-page word had a value by a relative frame
  image             a capture matches its private baseline per region, with
                    ignore rectangles and tolerances (exact or bounded)

Runs use the separately linked measurement fixture; the player executable is
never changed. Renderers:

  software  the framework's software rasteriser (game logic and guest image only;
            Nitrogen's OpenGL presentation is not exercised)
  mesa      OpenGL through a private, checksum-verified Mesa llvmpipe placed next to
            the fixture executable only (software-rendered, not GPU-representative)
  opengl    the machine's own OpenGL driver (hardware-rendered on a real GPU)

Baselines and captures are game-derived: they stay in the private .local folder
and never enter Git. Every result is labelled with the evidence it provides.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tools"))
import display_report as report  # noqa: E402

RENDERERS = ("software", "mesa", "opengl")
CHECKS = ("route_completed", "page_at", "image", "options_saved")
KINDS = ("present", "guest")
DEFAULT_OPTIONS = dict(video_mode=0, hud_scale=0, render_percent=100, master_volume=0, aspect=0, vsync=1,
                       hud_layout=0)
# nitrogen-options.ini keys in compat/nitrogen_options.h enum order (the index the
# fixture passes to nitrogen_option_set). A unit test checks this against the header.
OPTION_KEYS = ("video_mode", "hud_scale", "render_percent", "master_volume", "aspect", "vsync", "hud_layout",
               "fullscreen_refresh", "texture_filter", "dithering", "screen_model", "scanlines", "brightness")
PICTURE_PRESETS = ("original", "smooth", "crt", "composite")
MESA_FILES = ("opengl32.dll", "libgallium_wgl.dll")


# ---- scenarios -------------------------------------------------------------------
def validate(s: dict) -> list[str]:
    """Return the problems in a scenario; an empty list means it can run."""
    errors = []
    if not isinstance(s.get("id"), str) or not s["id"].replace("-", "").replace("_", "").isalnum():
        errors.append("id must be letters, digits, '-' or '_'")
    if s.get("renderer") not in RENDERERS:
        errors.append(f"renderer must be one of {RENDERERS}")
    w = s.get("window") or {}
    if w.get("mode") not in ("windowed", "none") or not all(isinstance(w.get(k), int) and w[k] >= 0
                                                          for k in ("width", "height")):
        errors.append("window needs mode windowed|none and integer width/height")
    if s.get("route", "default") not in ("default", "long"):
        errors.append("route must be default or long")
    if s.get("capture_kind", "present") not in KINDS:
        errors.append("capture_kind must be present or guest")
    if s.get("renderer") == "software" and s.get("capture_kind", "present") == "present":
        errors.append("the software renderer has no OpenGL presentation; use capture_kind guest")
    unknown = set(s.get("options") or {}) - set(OPTION_KEYS)
    if unknown:
        errors.append(f"unknown options {sorted(unknown)}")
    for item in s.get("set_options_at") or []:
        if not (isinstance(item, list) and len(item) == 3 and isinstance(item[0], int) and item[1] in OPTION_KEYS
                and isinstance(item[2], int)):
            errors.append("set_options_at entries are [frame, option key, integer value]")
    for item in s.get("preset_at") or []:
        if not (isinstance(item, list) and len(item) == 2 and isinstance(item[0], int) and item[1] in PICTURE_PRESETS):
            errors.append(f"preset_at entries are [frame, one of {PICTURE_PRESETS}]")
    restart = s.get("restart")
    if restart is not None and not (isinstance(restart, dict) and isinstance(restart.get("checks"), list)):
        errors.append("restart needs a checks list")
    captures = s.get("captures") or []
    if not all(isinstance(c, int) and c >= 0 for c in captures):
        errors.append("captures must be non-negative frame numbers")
    for c in (s.get("checks") or []) + ((s.get("restart") or {}).get("checks") or []):
        t = c.get("type")
        if t not in CHECKS:
            errors.append(f"unknown check {t!r}")
        elif t == "image":
            if c.get("capture") not in captures:
                errors.append(f"image check capture {c.get('capture')} is not captured")
            for r in (c.get("regions") or []) + (c.get("ignore") or []):
                if not isinstance(r, dict):
                    errors.append("regions/ignore entries must be objects")
                    continue
                if r in (c.get("regions") or []) and r.get("mask") not in (None, "ui", "world"):
                    errors.append("region mask must be ui or world")
                rect = r.get("rect", [0, 0, 1, 1] if r.get("mask") else None)
                if not (isinstance(rect, list) and len(rect) == 4 and 0 <= rect[0] < rect[2] <= 1
                        and 0 <= rect[1] < rect[3] <= 1):
                    errors.append("regions/ignore need rect [x0, y0, x1, y1] as fractions with x0<x1, y0<y1")
        elif t == "page_at" and not (isinstance(c.get("frame"), int) and isinstance(c.get("page"), int)):
            errors.append("page_at needs integer frame and page")
        elif t == "options_saved" and not (isinstance(c.get("values"), dict) and set(c["values"]) <= set(OPTION_KEYS)):
            errors.append("options_saved needs values {option key: integer}")
        if t == "image" and c.get("against") is not None and not isinstance(c.get("against"), str):
            errors.append("image against must be a scenario id")
    return errors


def load_scenarios(paths: list[Path], only: list[str]) -> list[dict]:
    found = []
    for p in paths:
        for f in sorted(p.glob("*.json")) if p.is_dir() else [p]:
            s = json.loads(f.read_text(encoding="utf-8"))
            if only and not any(o in s.get("id", "") for o in only):
                continue
            problems = validate(s)
            if problems:
                raise ValueError(f"{f.name}: " + "; ".join(problems))
            found.append(s)
    return found


# ---- image comparison --------------------------------------------------------------
def to_rect(frac: list[float], w: int, h: int) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = frac
    return int(round(x0 * w)), int(round(y0 * h)), max(int(round(x1 * w)), 1), max(int(round(y1 * h)), 1)


def _row_spans(x0: int, x1: int, y: int, ignore: list[tuple[int, int, int, int]]) -> list[tuple[int, int]]:
    spans = [(x0, x1)]
    for ix0, iy0, ix1, iy1 in ignore:
        if not iy0 <= y < iy1:
            continue
        nxt = []
        for a, b in spans:
            if ix1 <= a or ix0 >= b:
                nxt.append((a, b))
                continue
            if a < ix0:
                nxt.append((a, ix0))
            if ix1 < b:
                nxt.append((ix1, b))
        spans = nxt
    return spans


def compare_region(a: tuple, b: tuple, rect: list[float], ignore: list[list[float]] | None = None,
                   mask: bytes | None = None, want: int = 1) -> dict:
    """a/b are (w, h, channels, top-down pixels). Colour channels only (alpha ignored).
    With a UI mask (one byte per pixel, 1 = HUD/menu), only pixels whose mask
    value equals `want` count: want=1 compares the HUD, want=0 the world."""
    w, h, ch, pa = a
    if (b[0], b[1]) != (w, h) or (mask is not None and len(mask) != w * h):
        return dict(pixels=0, changed=0, changed_ratio=1.0, mean_abs=255.0, max_abs=255, size_mismatch=True)
    chb, pb = b[2], b[3]
    x0, y0, x1, y1 = to_rect(rect, w, h)
    masks = [to_rect(r, w, h) for r in (ignore or [])]
    pixels = changed = total = biggest = 0
    selector = bytes([want])
    for y in range(y0, y1):
        for sx0, sx1 in _row_spans(x0, x1, y, masks):
            n = sx1 - sx0
            if n <= 0:
                continue
            row_mask = mask[y * w + sx0:y * w + sx1] if mask is not None else None
            pixels += n if row_mask is None else row_mask.count(selector)
            ra = pa[(y * w + sx0) * ch:(y * w + sx1) * ch]
            rb = pb[(y * w + sx0) * chb:(y * w + sx1) * chb]
            if ch == chb and ra == rb:
                continue
            for i in range(n):
                if row_mask is not None and row_mask[i] != want:
                    continue
                d = max(abs(ra[i * ch + k] - rb[i * chb + k]) for k in range(3))
                if d:
                    changed += 1
                    total += d
                    biggest = max(biggest, d)
    return dict(pixels=pixels, changed=changed, changed_ratio=changed / pixels if pixels else 0.0,
                mean_abs=total / pixels if pixels else 0.0, max_abs=biggest, size_mismatch=False)


MASK_MAGIC = b"NUIM"
MASK_DRAW_DELAY = 2   # frame hooks between drawing a frame and presenting it


def make_mask(with_ui: tuple, without_ui: tuple, grow: int = 1) -> bytes:
    """1 where the frame drawn with Nitrogen's HUD/menu primitives differs from
    the same deterministic frame drawn without them, grown by `grow` pixels so
    filtered and blended UI edges are included."""
    w, h, ch, pa = with_ui
    if (without_ui[0], without_ui[1]) != (w, h):
        raise ValueError("mask frames differ in size")
    chb, pb = without_ui[2], without_ui[3]
    core = bytearray(w * h)
    for y in range(h):
        ra = pa[y * w * ch:(y + 1) * w * ch]
        rb = pb[y * w * chb:(y + 1) * w * chb]
        if ch == chb and ra == rb:
            continue
        for x in range(w):
            if any(ra[x * ch + k] != rb[x * chb + k] for k in range(3)):
                core[y * w + x] = 1
    if grow <= 0:
        return bytes(core)
    grown = bytearray(core)
    for y in range(h):
        row = core[y * w:(y + 1) * w]
        if 1 not in row:
            continue
        for x in range(w):
            if row[x]:
                for yy in range(max(0, y - grow), min(h, y + grow + 1)):
                    grown[yy * w + max(0, x - grow):yy * w + min(w, x + grow + 1)] = \
                        b"\x01" * (min(w, x + grow + 1) - max(0, x - grow))
    return bytes(grown)


def save_mask(path: Path, w: int, h: int, mask: bytes) -> None:
    import struct
    import zlib
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(MASK_MAGIC + struct.pack("<2I", w, h) + zlib.compress(mask, 9))


def load_mask(path: Path) -> tuple[int, int, bytes]:
    import struct
    import zlib
    data = path.read_bytes()
    if data[:4] != MASK_MAGIC:
        raise ValueError(f"Not a UI mask: {path.name}")
    w, h = struct.unpack_from("<2I", data, 4)
    mask = zlib.decompress(data[12:])
    if len(mask) != w * h:
        raise ValueError(f"Corrupt UI mask: {path.name}")
    return w, h, mask


def within(stats: dict, tolerance: dict) -> bool:
    if stats["size_mismatch"]:
        return False
    if tolerance.get("expect_change", False):
        return stats["changed"] > 0
    if tolerance.get("exact", False):
        return stats["changed"] == 0
    return (stats["changed_ratio"] <= tolerance.get("max_changed_ratio", 0.0)
            and stats["mean_abs"] <= tolerance.get("max_mean_abs", 0.0)
            and stats["max_abs"] <= tolerance.get("max_abs", 255))


def read_image(path: Path, bottom_up: bool) -> tuple:
    w, h, ch, px = report.read_raw(path)
    if bottom_up:
        stride = w * ch
        px = b"".join(px[(h - 1 - y) * stride:(h - y) * stride] for y in range(h))
    return w, h, ch, px


# ---- evidence ---------------------------------------------------------------------------
def evidence(renderer: str, gl_line: str, route_ok: bool) -> list[str]:
    """What a passing scenario demonstrates, in the owner's reporting vocabulary."""
    labels = ["integration-tested"]
    low = gl_line.lower()
    if renderer == "software":
        labels.append("software-rasterised (guest image; Nitrogen presentation not exercised)")
    elif "llvmpipe" in low or "softpipe" in low or renderer == "mesa":
        labels.append("software-rendered (Mesa llvmpipe; not GPU-representative)")
    elif gl_line and "microsoft" not in low and "gdi generic" not in low:
        labels.append("hardware-rendered")
    if route_ok:
        labels.append("gameplay-tested (scripted route)")
    return labels


# ---- running ---------------------------------------------------------------------------
def mesa_files(local: Path) -> dict[str, str]:
    """Expected SHA-256 of the private Mesa files, from the install-time manifest."""
    manifest = local / "tools" / "mesa" / "MANIFEST.txt"
    expected = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2 and len(parts[0]) == 64:
            expected[Path(parts[1].lstrip("*")).name] = parts[0]
    missing = [n for n in MESA_FILES if n not in expected]
    if missing:
        raise ValueError(f"Mesa manifest lacks {missing}")
    return expected


def place_mesa(measure, local: Path, exe: Path, player: Path) -> None:
    for name, digest in mesa_files(local).items():
        if name not in MESA_FILES:
            continue
        source = local / "tools" / "mesa" / "x64" / name
        if measure.sha256(source) != digest:
            raise ValueError(f"Private Mesa file changed since installation: {name}")
        shutil.copyfile(source, exe.parent / name)
    if any((player.parent / n).exists() for n in MESA_FILES):
        raise ValueError("Mesa files found beside the player executable; refusing to test")


def run_scenario(measure, info: dict, exe: Path, s: dict, out: Path, baselines: Path, update: bool,
                 hide_ui: str = "", restart_from: Path | None = None) -> dict:
    kind = s.get("capture_kind", "present")
    window = s["window"]
    run = dict(id=s["id"], kind=f"scenario ({s['renderer']})", mode=window["mode"], width=window["width"],
               height=window["height"], route=s.get("route", "default"), captures=s.get("captures") or [],
               golden=kind == "guest", renderer="software" if s["renderer"] == "software" else "opengl",
               vsync_via="option")
    suffix = {"": "", "shown": "-mask-ui", "hidden": "-mask-noui"}[hide_ui or ""]
    run_dir = out / "runs" / (s["id"] + suffix + ("-restart" if restart_from else ""))
    if run_dir.exists():
        shutil.rmtree(run_dir)
    (run_dir / "cards").mkdir(parents=True)
    config = measure.write_config(run, run_dir, None)
    if kind == "guest":
        text = config.read_text(encoding="utf-8").replace("present_capture 1", "present_capture 0")
        config.write_text(text, encoding="utf-8")
    # Mask runs: render worker off (frame-exact order) and, in the hidden run,
    # the HUD skipped in the captured frames only.
    # The frame presented at capture N was drawn two frame hooks earlier (the
    # game double-buffers; measured on 1024x768: offsets 0, 1, 3 and 4 leave the
    # HUD in place, 2 removes it), so the HUD is hidden in exactly that frame.
    extra = ([f"hide_ui_rel {f - MASK_DRAW_DELAY}" for f in run["captures"] if f > MASK_DRAW_DELAY]
             if hide_ui == "hidden" else [])
    if not restart_from:
        extra += [f"option_rel {f} {OPTION_KEYS.index(k)} {v}" for f, k, v in s.get("set_options_at") or []]
        extra += [f"preset_rel {f} {PICTURE_PRESETS.index(n)}" for f, n in s.get("preset_at") or []]
    if extra:
        config.write_text(config.read_text(encoding="utf-8") + "".join(x + "\n" for x in extra), encoding="utf-8")
    if restart_from:            # a real restart: the options file the first run saved, unchanged
        shutil.copyfile(restart_from, run_dir / "nitrogen-options.ini")
    else:
        options = dict(DEFAULT_OPTIONS, **(s.get("options") or {}))
        (run_dir / "nitrogen-options.ini").write_text("".join(f"{k} {v}\n" for k, v in options.items()),
                                                      encoding="utf-8")
    from run_runtime import runtime_environment
    env = runtime_environment(info["work"], render_worker=False if hide_ui else None)
    env.pop("PSX_VSYNC", None)
    env.update(NITROGEN_MEASURE_CONFIG=str(config), NITROGEN_OPTIONS_FILE=str(run_dir / "nitrogen-options.ini"),
               NITROGEN_NATIVE_DISC_BIN=str(info["disc_bin"]),
               PSX_OVERLAY_CAPTURES=str(run_dir / "overlay_captures.json"),
               PSX_OVERLAY_CAPTURE_ROOT=str(run_dir / "overlay-private"))
    if s["renderer"] == "software":
        env["SDL_RENDER_DRIVER"] = "software"
    if s["renderer"] == "mesa":            # this process only; never the system or the player
        env.update(GALLIUM_DRIVER="llvmpipe", LP_NUM_THREADS=str(s.get("mesa_threads", 32)))
    command = [str(exe), "--game", str(info["game"]), "--no-launcher", "--renderer", run["renderer"],
               "--memcard-dir", str(run_dir / "cards")]
    started = time.monotonic()
    with (run_dir / "runtime.log").open("w", encoding="utf-8", errors="replace") as log:
        try:
            code = subprocess.run(command, cwd=run_dir, env=env, stdout=log, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL, timeout=s.get("timeout", 7200)).returncode
        except subprocess.TimeoutExpired:
            code = "timeout"
    seconds = round(time.monotonic() - started, 1)
    result = measure.analyse_run(run, run_dir, code, seconds, True)
    log_text = (run_dir / "runtime.log").read_text(encoding="utf-8", errors="replace")
    gl_line = next((l for l in log_text.splitlines() if "OpenGL context created" in l), "")
    route_ok = result["status"] == "completed"
    checks = []
    fixture = report.read_info(run_dir / "fixture-info.json")
    anchor = (fixture.get("timing") or {}).get("measure_anchor") or 0
    rows = report.read_csv(run_dir / "frames.csv")
    for c in ((s.get("restart") or {}).get("checks") or []) if restart_from else (s.get("checks") or []):
        if c["type"] == "options_saved":
            saved = {}
            for line in (run_dir / "nitrogen-options.ini").read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1].lstrip("-").isdigit():
                    saved[parts[0]] = int(parts[1])
            wrong = {k: saved.get(k) for k, v in c["values"].items() if saved.get(k) != v}
            checks.append(dict(type="options_saved", passed=not wrong,
                               detail="saved as expected" if not wrong else f"saved values differ: {wrong}"))
        elif c["type"] == "route_completed":
            checks.append(dict(type="route_completed", passed=route_ok, detail=result.get("reason") or "completed"))
        elif c["type"] == "page_at":
            pages = [r["page"] for r in rows if anchor and r["frame"] - anchor <= c["frame"]]
            ok = bool(pages) and pages[-1] == c["page"]
            checks.append(dict(type="page_at", passed=ok, detail=f"page {pages[-1] if pages else None} by "
                                                                    f"frame {c['frame']}, expected {c['page']}"))
        else:
            name = f"{kind}-{c['capture']}.raw"
            captured = run_dir / name
            own = baselines / s.get("baseline_set", "original") / s["renderer"] / (s["id"] + ("-restart" if restart_from else "")) / name
            against = c.get("against")
            base = (baselines / c.get("against_set", "original") / s["renderer"] / against / name) if against else own
            if not captured.is_file():
                checks.append(dict(type="image", capture=c["capture"], passed=False, detail="capture missing"))
                continue
            if hide_ui == "shown":
                checks.append(dict(type="image", capture=c["capture"], passed=None, detail="mask reference frame"))
                continue
            if hide_ui == "hidden":
                shown = out / "runs" / (s["id"] + "-mask-ui") / name
                if not (base.is_file() and shown.is_file()):
                    checks.append(dict(type="image", capture=c["capture"], passed=None,
                                       detail="no mask: baseline or reference frame missing"))
                    continue
                with_ui = read_image(shown, kind == "present")
                mask = make_mask(with_ui, read_image(captured, kind == "present"))
                save_mask(base.with_suffix(".uimask"), with_ui[0], with_ui[1], mask)
                checks.append(dict(type="image", capture=c["capture"], passed=None,
                                   detail=f"UI mask recorded ({mask.count(1)} of {len(mask)} pixels)"))
                continue
            if update and not against:
                base.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(captured, base)
                checks.append(dict(type="image", capture=c["capture"], passed=None, detail="baseline updated"))
                continue
            if not base.is_file():
                checks.append(dict(type="image", capture=c["capture"], passed=None, detail="not tested: no baseline"))
                continue
            bottom_up = kind == "present"
            a, b = read_image(captured, bottom_up), read_image(base, bottom_up)
            regions = []
            mask_file = base.with_suffix(".uimask")
            mask = load_mask(mask_file)[2] if mask_file.is_file() else None
            for r in c.get("regions") or [dict(name="whole", rect=[0, 0, 1, 1])]:
                if r.get("mask") and mask is None:
                    regions.append(dict(name=r.get("name", "region"), passed=None, pixels=0, changed=0,
                                        changed_ratio=0.0, mean_abs=0.0, max_abs=0, size_mismatch=False,
                                        detail="not tested: no UI mask (run --make-masks)"))
                    continue
                stats = compare_region(a, b, r.get("rect", [0, 0, 1, 1]), [i["rect"] for i in c.get("ignore") or []],
                                       mask if r.get("mask") else None, 1 if r.get("mask") == "ui" else 0)
                regions.append(dict(name=r.get("name", "region"), passed=within(stats, c.get("tolerance") or
                                                                                 {"exact": True}), **stats))
            checks.append(dict(type="image", capture=c["capture"],
                               passed=(None if any(r["passed"] is None for r in regions) else
                                       all(r["passed"] for r in regions)),
                               regions=regions))
    # Every check must have run and passed. A skipped or recorded image check
    # leaves the scenario unverified rather than passed.
    recorded = any(c.get("detail") == "baseline updated" or str(c.get("detail", "")).startswith("UI mask recorded")
                   for c in checks)
    passed = route_ok and bool(checks) and all(c["passed"] is True for c in checks)
    if not restart_from and s.get("restart") and route_ok:
        second = run_scenario(measure, info, exe, s, out, baselines, update, restart_from=run_dir / "nitrogen-options.ini")
        checks.append(dict(type="restart", passed=second["passed"] if not update else None,
                           detail=f"second launch from the saved options: {'pass' if second['passed'] else second['status']}",
                           restart=second))
    return dict(id=s["id"] + ("-restart" if restart_from else ""), description=s.get("description", ""),
                renderer=s["renderer"], gl=gl_line,
                window=[window["width"], window["height"]], seconds=seconds, status=result["status"],
                reason=result.get("reason"), passed=passed, checks=checks,
                evidence=(evidence(s["renderer"], gl_line, route_ok) if passed else
                          ["baseline recorded; not verified"] if recorded else ["not verified"]),
                recorded=recorded,
                untested=[c for c in checks if c["passed"] is None])


def markdown(results: list[dict], started: str) -> str:
    lines = [f"# Nitrogen scenario report", "", f"Started {started}. Captures and baselines stay private in .local.",
             "", "| Scenario | Renderer | Window | Result | Evidence | Time |", "| --- | --- | --- | --- | --- | --- |"]
    for r in results:
        failed = r["status"] != "completed" or any(c["passed"] is False for c in r["checks"])
        verdict = ("pass" if r["passed"] else "FAIL" if failed else
                   "baseline recorded" if r.get("recorded") else "not tested")
        lines.append(f"| {r['id']} | {r['renderer']} | {r['window'][0]}x{r['window'][1]} | {verdict} | "
                     f"{'; '.join(r['evidence'])} | {r['seconds']} s |")
    lines += ["", "## Checks", ""]
    for r in results:
        lines.append(f"### {r['id']}")
        if r["gl"]:
            lines.append(f"- Renderer: {r['gl'].split('created', 1)[-1].strip()}")
        for c in r["checks"]:
            mark = {True: "pass", False: "FAIL", None: "not tested"}[c["passed"]]
            if c["type"] == "image" and c.get("regions"):
                for g in c["regions"]:
                    if g["passed"] is None:
                        lines.append(f"- image {c['capture']} [{g['name']}]: {g.get('detail', 'not tested')}")
                        continue
                    lines.append(f"- image {c['capture']} [{g['name']}]: {'pass' if g['passed'] else 'FAIL'} "
                                 f"(changed {g['changed']}/{g['pixels']}, mean {g['mean_abs']:.3f}, max {g['max_abs']})")
            else:
                lines.append(f"- {c['type']}{' ' + str(c.get('capture')) if 'capture' in c else ''}: {mark} "
                             f"({c.get('detail', '')})")
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--installation", type=Path, default=ROOT)
    p.add_argument("--scenarios", type=Path, action="append", default=[])
    p.add_argument("--only", action="append", default=[])
    p.add_argument("--update-baselines", action="store_true",
                   help="Record this run's captures as the private baselines instead of comparing")
    p.add_argument("--make-masks", action="store_true",
                   help="Render each scenario with Nitrogen's HUD/menu primitives hidden and record UI masks "
                        "against its existing baselines")
    p.add_argument("--validate-only", action="store_true", help="Check the scenario files and stop")
    args = p.parse_args(argv)
    scenarios = load_scenarios(args.scenarios or [HERE / "scenarios"], args.only)
    if args.validate_only:
        print(f"{len(scenarios)} scenario(s) valid")
        return 0
    import measure_display as measure
    info = measure.locate(args.installation.resolve())
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    out = measure.private(info["local"] / "tests" / time.strftime("n2test-%Y%m%d-%H%M%S"))
    out.mkdir(parents=True)
    baselines = info["local"] / "test-baselines"
    exe = measure.build_fixture(info, out)
    if any(s["renderer"] == "mesa" for s in scenarios):
        place_mesa(measure, info["local"], exe, info["executable"])
    results = []
    for s in scenarios:
        if args.make_masks:     # same frames with and without the HUD, worker off for both
            run_scenario(measure, info, exe, s, out, baselines, False, "shown")
            r = run_scenario(measure, info, exe, s, out, baselines, False, "hidden")
        else:
            r = run_scenario(measure, info, exe, s, out, baselines, args.update_baselines)
        results.append(r)
        state = "pass" if r["passed"] else "baseline recorded" if r.get("recorded") else "not verified"
        print(f"{r['id']}: {state} [{r['status']}] ({r['seconds']} s)", flush=True)
    (out / "report.json").write_text(json.dumps(dict(started=started, results=results), indent=1), encoding="utf-8")
    (out / "report.md").write_text(markdown(results, started), encoding="utf-8")
    print(out / "report.md")
    if args.update_baselines or args.make_masks:
        return 0 if all(r["status"] == "completed" for r in results) else 1
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
