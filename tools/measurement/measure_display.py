#!/usr/bin/env python3
"""GPL-3.0-or-later. Measure Nitrogen's display behaviour on this PC (V0 baseline).

Links tools/measurement/display_fixture.cpp separately against the existing
normal (diagnostics-off) game build, then runs the ordinary controller route at
each planned window size, display mode, world render percentage and VSync
setting. The player's executable, settings, saves, mods and disc are never
changed: the fixture runs from a private copy with its own options, memory
cards and menu preferences. All output stays under <install>/.local/measurements.

    python tools/measurement/measure_display.py --installation "D:/Games/Nitrogen-Squared"
    python tools/measurement/measure_display.py --preset quick
    python tools/measurement/measure_display.py --list-displays

Presets: probe (displays only), golden (two identical runs for determinism, about
7 minutes), quick (about 35 minutes), full (about 95 minutes), stutter (three long
routes, about 30 minutes), discover (maintenance: dense captures along the route).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tools"))
import display_report as report  # noqa: E402
import windows_displays  # noqa: E402

FIXTURE_NAME = "NitrogenSquared-display-measure"
WRAPS = ("SDL_PollEvent", "SDL_GL_SwapWindow", "SDL_RenderPresent")


class MeasureError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def private(path: Path) -> Path:
    path = path.resolve()
    if ".local" not in path.parts:
        raise MeasureError(f"Measurement output must be inside a .local folder: {path}")
    return path


# ---- installation -------------------------------------------------------------
def locate(installation: Path) -> dict:
    """The normal build, its CMake cache, the verified import and the data-track BIN."""
    local = installation.resolve() / ".local"
    work = local / "runtime-dev"
    try:
        receipt = json.loads((work / "runtime-build.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise MeasureError(f"No completed game build in {work}. Prepare and play the game once first.") from None
    if receipt.get("diagnostics_enabled") is not False:
        raise MeasureError("A normal (diagnostics-off) game build is required; this build has diagnostics enabled.")
    build = Path(receipt["executable"]).parent
    cache = {}
    for line in (build / "CMakeCache.txt").read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.startswith(("//", "#")):
            cache[key.split(":", 1)[0]] = value
    imported = json.loads((Path(receipt["installation"]) / "installation.json").read_text(encoding="utf-8"))
    return dict(local=local, work=work, build=build, receipt=receipt, cache=cache, imported=imported,
                executable=Path(receipt["executable"]), game=work / "game.toml",
                framework=Path(cache.get("PSXRECOMP_ROOT", local / "psxrecomp")),
                disc_bin=Path(imported["bin"]))


def link_command(info: dict) -> tuple[list[str], str]:
    """The production link step only (no post-link copies into the build)."""
    build = info["build"]
    generator = info["cache"].get("CMAKE_GENERATOR", "")
    if "Ninja" in generator:
        ninja = info["cache"].get("CMAKE_MAKE_PROGRAM", "ninja")
        text = subprocess.run([ninja, "-C", str(build), "-t", "commands", "nitrogen-runtime"], check=True,
                              capture_output=True, text=True).stdout.strip().splitlines()[-1]
    else:
        text = (build / "CMakeFiles/nitrogen-runtime.dir/link.txt").read_text(encoding="utf-8").strip()
    return split_link(text), generator


def split_link(text: str) -> list[str]:
    """Extract and tokenize the compiler-driver link from a Ninja/Make command line."""
    text = text.strip()
    if text.lower().startswith(("cmd.exe /c", "c:\\windows\\system32\\cmd.exe /c")):
        text = text.split("/C", 1)[1].strip().strip('"') if "/C" in text else text
    for prefix in ("cd . && ", ": && "):
        if text.startswith(prefix):
            text = text[len(prefix):]
    text = text.split(" && ")[0].strip()
    if os.name == "nt":
        parts = [p[1:-1] if len(p) > 1 and p[0] == p[-1] == '"' else p for p in shlex.split(text, posix=False)]
    else:
        parts = shlex.split(text)
    if "-o" not in parts:
        raise MeasureError("Unexpected production link command: no -o output")
    if any("--wrap=" in p for p in parts):
        raise MeasureError("The production link already contains interposition; refusing to measure it")
    return parts


def _ninja_lines(path: Path) -> list[str]:
    """Logical lines of a ninja file: a line ending in an unescaped `$` continues."""
    out, pending = [], None
    for raw in path.read_text(encoding="utf-8").splitlines():
        text = raw if pending is None else pending + " " + raw.lstrip()
        body = text.rstrip()
        trailing = len(body) - len(body.rstrip("$"))
        if trailing % 2 == 1:
            pending = body[:-1]
            continue
        out.append(text)
        pending = None
    return out


def _ninja_split(text: str) -> list[str]:
    """Split on unescaped spaces; unescape `$ `, `$:` and `$$` (variables are kept)."""
    words, word, i = [], "", 0
    while i < len(text):
        c = text[i]
        if c == "$" and i + 1 < len(text) and text[i + 1] in " :$":
            word += text[i + 1]
            i += 2
            continue
        if c == " ":
            if word:
                words.append(word)
            word = ""
        else:
            word += c
        i += 1
    if word:
        words.append(word)
    return words


def _ninja_block(lines: list[str], start: int) -> dict:
    variables = {}
    for line in lines[start + 1:]:
        if not line[:1].isspace():
            break
        key, _, value = line.strip().partition(" = ")
        variables[key] = value
    return variables


def ninja_response_file(build: Path, target: str) -> tuple[str, str] | None:
    """(rsp name, rebuilt rsp content) for the link edge that produces `target`.

    Ninja writes a response file only while it links and deletes it afterwards,
    so rebuild it from the edge's explicit inputs and the rule's rspfile_content."""
    lines = _ninja_lines(build / "build.ninja")
    escaped = target.replace("$", "$$").replace(" ", "$ ").replace(":", "$:")
    prefix = f"build {escaped}:"
    start = next((i for i, line in enumerate(lines) if line.startswith(prefix)), None)
    if start is None:
        return None
    words = _ninja_split(lines[start][len(prefix):].strip())
    rule, explicit = words[0], []
    for word in words[1:]:
        if word in ("|", "||", "|@"):
            break
        explicit.append(word)
    variables = _ninja_block(lines, start)
    content = None
    for name in ("build.ninja", "CMakeFiles/rules.ninja", "rules.ninja"):
        path = build / name
        if path.is_file():
            rules = _ninja_lines(path)
            at = next((i for i, line in enumerate(rules) if line.strip() == f"rule {rule}"), None)
            if at is not None:
                content = _ninja_block(rules, at).get("rspfile_content")
                break
    if content is None or "RSP_FILE" not in variables:
        return None

    def quote(word: str) -> str:
        return f'"{word}"' if " " in word else word

    parts = []
    for token in content.split():
        if token in ("$in", "$in_newline"):
            parts.extend(quote(x) for x in explicit)
        elif token.startswith("$"):
            parts.append(variables.get(token[1:], ""))
        else:
            parts.append(token)
    return variables["RSP_FILE"], " ".join(x for x in parts if x) + "\n"


def build_fixture(info: dict, out: Path) -> Path:
    """Compile the fixture and relink the production objects into a private executable."""
    bin_dir = out / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    production_before = sha256(info["executable"])
    compiler = info["cache"].get("CMAKE_CXX_COMPILER") or "c++"
    sdl = Path(info["cache"]["SDL3_SOURCE_DIR"]) / "include"
    obj = bin_dir / "display_fixture.o"
    flags = shlex.split(info["cache"].get("CMAKE_CXX_FLAGS", ""), posix=os.name != "nt")
    subprocess.run([compiler, *flags, "-std=c++17", "-O2", "-DPSX_SDL3=1", "-I" + str(info["framework"] / "runtime/include"),
                    "-I" + str(sdl), "-c", str(HERE / "display_fixture.cpp"), "-o", str(obj)], check=True)
    command, _ = link_command(info)
    exe = bin_dir / (FIXTURE_NAME + (".exe" if os.name == "nt" else ""))
    command[command.index("-o") + 1] = str(exe)
    command = [("-Wl,--out-implib," + str(bin_dir / "libdisplay-measure.dll.a"))
               if c.startswith("-Wl,--out-implib,") else c for c in command]
    response = [c for c in command if c.startswith("@")]
    if response:
        rebuilt = ninja_response_file(info["build"], info["executable"].name)
        if not rebuilt:
            raise MeasureError("Could not rebuild the production link response file from build.ninja")
        name, content = rebuilt
        (bin_dir / "link.rsp").write_text(content, encoding="utf-8")
        command = [("@" + str(bin_dir / "link.rsp")) if c == "@" + name else c for c in command]
    command[1:1] = ["-Wl," + ",".join("--wrap=" + w for w in WRAPS), str(obj)]
    subprocess.run(command, cwd=info["build"], check=True)
    if sha256(info["executable"]) != production_before:
        raise MeasureError("The production executable changed while linking the fixture")
    # Files the runtime reads beside its executable. Player bindings, mods and
    # menu preferences are deliberately not copied: runs use defaults.
    for name in ("bios", "assets", "cache"):
        source = info["build"] / name
        if source.is_dir():
            shutil.copytree(source, bin_dir / name, dirs_exist_ok=True)
    manifest = info["executable"].with_name(info["executable"].stem + ".execution.json")
    if manifest.is_file():
        shutil.copyfile(manifest, exe.with_name(exe.stem + ".execution.json"))
    # Hide the F1 menu bar so the game area equals the requested size.
    (bin_dir / "nitrogen-menu.ini").write_text("visible 0\nautohide 0\nperformance 0\naudio 0\n", encoding="utf-8")
    return exe


# ---- one run --------------------------------------------------------------------
def write_config(run: dict, run_dir: Path, display_index: int | None) -> Path:
    lines = [f"out {run_dir}", f"mode {run.get('mode', 'none')}",
             f"width {run.get('width', 0)}", f"height {run.get('height', 0)}",
             f"refresh {run.get('refresh', 0)}", f"display {display_index if display_index is not None else -1}",
             "apply 120", f"probe {1 if run.get('probe') else 0}",
             f"guest_capture {1 if run.get('golden') or run.get('route') == 'long' else 0}",
             f"present_capture {0 if run.get('probe') else 1}"]
    if run.get("probe"):
        lines.append("stop 400")
    else:
        route = report.route_config(run.get("route", "default"))
        # Absolute ceiling: every awaited screen at its limit plus the measured span.
        lines.append(f"stop {report.WAIT_LIMIT * len(route['stages']) + route['stop_rel'] + 600}")
        lines.append(f"stop_rel {route['stop_rel']}")
        for stage in route["stages"]:
            lines.append(f"stage {stage['kind']} {stage['a']} {stage['b']} {stage['delay']} {stage['timeout']} "
                         f"{stage['repeat']}")
            lines += [f"step {b} {f}" for b, f in stage["steps"]]
        lines += [f"capture_rel {f}" for f in run.get("captures") or route["captures_rel"]]
        lines += [f"scale_rel {f}" for f in route["scale_rel"]]
        lines += ["scale_sample 200"]
    path = run_dir / "measure.cfg"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_once(exe: Path, info: dict, run: dict, out: Path, display_index: int | None, timeout: float,
             audible: bool, keep_raw: bool) -> dict:
    run_dir = out / "runs" / run["id"]
    if run_dir.exists():
        shutil.rmtree(run_dir)
    (run_dir / "cards").mkdir(parents=True)
    config = write_config(run, run_dir, display_index)
    (run_dir / "nitrogen-options.ini").write_text(
        f"video_mode {run.get('video_mode', 0)}\nhud_scale 0\nrender_percent {run.get('render_percent', 100)}\n"
        f"master_volume {100 if audible else 0}\naspect {run.get('aspect_option', 0)}\nvsync {run.get('vsync', 1)}\n"
        f"hud_layout {run.get('hud_layout', 0)}\n",
        encoding="utf-8")
    from run_runtime import runtime_environment
    env = runtime_environment(info["work"], render_worker=None)
    env.update(NITROGEN_MEASURE_CONFIG=str(config), NITROGEN_OPTIONS_FILE=str(run_dir / "nitrogen-options.ini"),
               NITROGEN_NATIVE_DISC_BIN=str(info["disc_bin"]),
               PSX_OVERLAY_CAPTURES=str(run_dir / "overlay_captures.json"),
               PSX_OVERLAY_CAPTURE_ROOT=str(run_dir / "overlay-private"))
    # V0 runs set VSync for the process (as before); V1 runs use the player's
    # own VSync setting, so the developer override must be absent.
    env.pop("PSX_VSYNC", None)
    if run.get("vsync_via", "env") == "env":
        env["PSX_VSYNC"] = str(run.get("vsync", 1))
    if run.get("renderer") == "software":
        env["SDL_RENDER_DRIVER"] = "software"   # tooling checks on hosts without OpenGL 3
    command = [str(exe), "--game", str(info["game"]), "--no-launcher", "--renderer", run.get("renderer", "opengl"),
               "--memcard-dir", str(run_dir / "cards")]
    started = time.monotonic()
    with (run_dir / "runtime.log").open("w", encoding="utf-8", errors="replace") as log:
        try:
            code = subprocess.run(command, cwd=run_dir, env=env, stdout=log, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL, timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            code = "timeout"
    result = analyse_run(run, run_dir, code, round(time.monotonic() - started, 1), keep_raw)
    return result


def analyse_run(run: dict, run_dir: Path, code, seconds: float, keep_raw: bool) -> dict:
    fixture = report.read_info(run_dir / "fixture-info.json")
    frames = report.read_csv(run_dir / "frames.csv")
    swaps = report.read_csv(run_dir / "swaps.csv")
    timing = fixture.get("timing", {})
    after = fixture.get("window_after") or {}
    apply = fixture.get("apply") or {}
    status, reason = "completed", None
    if apply and not apply.get("ok"):
        status, reason = "not tested", apply.get("reason") or "the window/display change was refused"
    elif code not in (0,):
        status, reason = "failed", f"exit {code}; see runtime.log"
    elif timing.get("route_failed"):
        status, reason = "failed", "an awaited screen did not appear; see the stage anchors and captures"
    elif not frames:
        status, reason = "failed", "no frames were recorded"
    elif not run.get("probe"):
        stop_rel = report.route_config(run.get("route", "default"))["stop_rel"]
        anchor = timing.get("measure_anchor") or 0
        if not anchor or frames[-1]["frame"] < anchor + stop_rel - 5:
            status, reason = "failed", "the route did not reach its last frame"
    samples = fixture.get("window_sample") or []
    actual = (samples[-1] if samples else after) or {}
    pixels = actual.get("pixels") or [0, 0]
    reserved = actual.get("menu_reserved") or 0
    if status == "completed" and run.get("width") and run.get("mode") in ("windowed", "exclusive"):
        if list(pixels) != [run["width"], run["height"]]:
            status, reason = "size mismatch", (f"requested {run['width']}x{run['height']} but the window had "
                                               f"{pixels[0]}x{pixels[1]} pixels; not counted as tested at that size")
    display_mode = actual.get("display_mode") or {}
    refresh = (display_mode or {}).get("refresh") or None
    scale = (fixture.get("scale_sample") or [None])[-1]
    world = tuple(actual.get("world_raster") or (0, 0))
    gl = fixture.get("gl") or {}
    captures = convert_captures(run_dir, fixture, keep_raw)
    return dict(
        id=run["id"], kind=run["kind"], status=status, reason=reason, seconds=seconds,
        requested={k: run.get(k) for k in ("width", "height", "refresh", "aspect", "render_percent", "vsync",
                                          "video_mode", "route", "aspect_option", "vsync_via", "hud_layout")},
        actual=dict(window_pixels=pixels, game_area=[pixels[0], pixels[1] - reserved] if pixels else None,
                    menu_bar_reserved=reserved, fullscreen=actual.get("fullscreen"),
                    display_mode=display_mode, window_mode=actual.get("window_mode"),
                    ui_step=actual.get("ui_step"), world_raster=list(world),
                    scale=scale, display_status=(fixture.get("display_status") or [None])[-1]),
        frames=report.frame_analysis(frames, timing, swaps) if frames else None,
        presentation=report.presentation_analysis(swaps, refresh, timing.get("present_kind", 0),
                                                  report.scene_map(frames, timing, swaps) if frames else None),
        memory=dict(nominal=report.nominal_memory(scale, world, (scale or {}).get("max_dim") or gl.get("max_texture_size")),
                    measured=report.measured_memory(fixture.get("memory_sample") or [])),
        captures=captures, gl=gl)


def convert_captures(run_dir: Path, fixture: dict, keep_raw: bool) -> dict:
    hashes = {}
    for kind, bottom_up in (("guest_capture", False), ("present_capture", True)):
        for item in fixture.get(kind) or []:
            raw = run_dir / (item.get("file") or "")
            if not item.get("file") or not raw.is_file():
                continue
            w, h, channels, pixels = report.read_raw(raw)
            name = raw.with_suffix(".png").name
            hashes[name] = report.write_png(run_dir / name, w, h, channels, pixels, bottom_up)
            if not keep_raw:
                raw.unlink()
    return hashes


# ---- driver ---------------------------------------------------------------------
def host_details() -> dict:
    return dict(os=platform.platform(), machine=platform.machine(), processor=platform.processor(),
                logical_cpus=os.cpu_count(), python=platform.python_version())


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--installation", type=Path, default=ROOT,
                   help="Folder containing .local (a portable Nitrogen-Squared folder or this source checkout)")
    p.add_argument("--preset", choices=["probe", "discover", "golden", "quick", "full", "stutter", "v1"],
                   default="quick")
    p.add_argument("--baseline", type=Path,
                   help="An earlier report.json (for example V0's) to compare the golden screenshots with")
    p.add_argument("--display", type=int, help="SDL display index to test (default: the widest display)")
    p.add_argument("--only", action="append", default=[], help="Run only plan entries whose id contains this text")
    p.add_argument("--renderer", choices=["opengl", "software"], default="opengl",
                   help="software exists only to check the tooling on hosts without OpenGL 3")
    p.add_argument("--timeout", type=float, default=900.0, help="Seconds allowed per run")
    p.add_argument("--audible", action="store_true", help="Keep game audio at full volume (muted by default)")
    p.add_argument("--keep-raw", action="store_true", help="Keep raw captures beside their PNG files")
    p.add_argument("--list-displays", action="store_true", help="Print the Windows display inventory and exit")
    p.add_argument("--yes", action="store_true", help="Start without the confirmation prompt")
    args = p.parse_args(argv)
    if args.list_displays:
        print(json.dumps(windows_displays.inventory(), indent=2))
        return 0
    try:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline else None
        info = locate(args.installation)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        out = private(info["local"] / "measurements" / f"display-{stamp}")
        out.mkdir(parents=True)
        print(f"Output: {out}", flush=True)
        print("Linking the measurement executable against the normal build objects…", flush=True)
        exe = build_fixture(info, out)
        probe = dict(id="probe", kind="probe", probe=True, mode="none", renderer=args.renderer)
        probe_result = run_once(exe, info, probe, out, args.display, 300, args.audible, args.keep_raw)
        fixture = report.read_info(out / "runs/probe/fixture-info.json")
        displays = fixture.get("displays") or []
        display = report.pick_display(displays, args.display)
        if display is None:
            raise MeasureError("The probe run reported no displays; see runs/probe/runtime.log")
        runs = [] if args.preset == "probe" else report.plan_runs(display, args.preset)
        if args.only:
            runs = [r for r in runs if any(o in r["id"] for o in args.only)]
        for r in runs:
            r["renderer"] = args.renderer
        print(f"Testing display {display['index']} '{display.get('name')}' "
              f"{display['bounds'][2]}x{display['bounds'][3]}; {len(runs)} runs, "
              f"about {report.estimate_minutes(runs)} minutes.", flush=True)
        if runs and not args.yes and sys.stdin.isatty():
            input("Close other games and heavy applications, then press Enter to start (Ctrl+C cancels)…")
        results = [probe_result]
        for i, run in enumerate(runs, 1):
            print(f"[{i}/{len(runs)}] {run['id']} ({run['kind']})", flush=True)
            results.append(run_once(exe, info, run, out, display["index"], args.timeout, args.audible, args.keep_raw))
            print(f"    {results[-1]['status']}" + (f": {results[-1]['reason']}" if results[-1]["reason"] else ""),
                  flush=True)
        goldens = [r for r in results if "-golden-" in r["id"] and r["status"] == "completed"]
        determinism = (report.compare_goldens(goldens[0]["captures"], goldens[1]["captures"])
                       if len(goldens) == 2 else dict(deterministic=None, reason="two completed golden runs are needed"))
        document = dict(
            schema=1, tool="tools/measurement/measure_display.py", created=dt.datetime.now().astimezone().isoformat(),
            preset=args.preset, renderer=args.renderer, host=host_details(),
            build=dict(version=(ROOT / "VERSION").read_text().strip() if (ROOT / "VERSION").is_file() else None,
                       production_executable_sha256=sha256(info["executable"]),
                       diagnostics_enabled=info["receipt"].get("diagnostics_enabled"),
                       build_identity=info["receipt"].get("build_identity")),
            gpu=next((r["gl"] for r in results if r.get("gl")), None),
            sdl_displays=displays, tested_display=display, windows_displays=windows_displays.inventory(),
            golden_determinism=determinism, runs=results,
            baseline_comparison=report.compare_baseline(baseline, results) if baseline else None,
            limits=["Present times are when the swap call returned, not scanout; compositor queueing is invisible.",
                    "Windowed sizes larger than the display are rendered at that size but not fully visible; "
                    "they are reported as windowed-oversized, not as tested display modes.",
                    "Exclusive modes are only tested when the display lists them.",
                    "Scene labels follow the controller schedule; captures confirm what was on screen."])
        (out / "report.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        (out / "summary.md").write_text(summary(document), encoding="utf-8")
        print(f"Report: {out / 'report.json'}\nSummary: {out / 'summary.md'}", flush=True)
        return 0
    except (MeasureError, OSError, ValueError, KeyError, subprocess.CalledProcessError) as e:
        print(f"Display measurement failed: {e}", file=sys.stderr)
        return 1


def summary(doc: dict) -> str:
    lines = ["# Display measurement summary", "", f"Created {doc['created']} · preset {doc['preset']} · "
             f"renderer {doc['renderer']}", ""]
    gpu = doc.get("gpu")
    lines.append(f"GPU: {gpu.get('renderer')} · OpenGL {gpu.get('version')} · max texture {gpu.get('max_texture_size')}"
                 if gpu else "GPU: not measured (no OpenGL presents in these runs)")
    d = doc.get("tested_display") or {}
    lines += [f"Display: {d.get('name')} {d.get('bounds')} desktop {d.get('desktop')}", "",
              f"Golden determinism: {doc['golden_determinism']}", "",
              *([f"Compared with baseline: {doc['baseline_comparison']}", ""] if doc.get("baseline_comparison") else []),
              "| Run | Status | Window | Display mode | 3D raster | Game fps (gameplay) | Game present interval p50/p99 ms | Hitches | Hitches by scene |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in doc["runs"]:
        frames = r.get("frames") or {}
        f = frames.get("scenes", {}).get("level-1-gameplay", {})
        pres = r.get("presentation") or {}
        iv = pres.get("interval_ms") or {}
        mode = (r["actual"].get("display_mode") or {})
        per_scene: dict[str, int] = {}
        for h in frames.get("hitches") or []:
            per_scene[h["scene"]] = per_scene.get(h["scene"], 0) + 1
        breakdown = ", ".join(f"{k} {v}" for k, v in per_scene.items()) or "none"
        lines.append(f"| {r['id']} | {r['status']} | {r['actual'].get('window_pixels')} | "
                     f"{mode.get('w')}x{mode.get('h')}@{mode.get('refresh')} | {r['actual'].get('world_raster')} | "
                     f"{f.get('game_frame_rate')} | {iv.get('p50')}/{iv.get('p99')} | "
                     f"{frames.get('hitch_count')} | {breakdown} |")
    v1 = [r for r in doc["runs"] if (r.get("actual") or {}).get("display_status")]
    if v1:
        lines += ["", "| Run | Aspect option | HUD layout | Window | Game area | VSync option/effective | 3D requested | 3D actual | 3D limit |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        reasons = {0: "full size", 1: "texture limit", 2: "allocation refused", 3: "not used"}
        for r in v1:
            s = r["actual"]["display_status"]
            lines.append(f"| {r['id']} | {s.get('aspect_option')} | {s.get('hud_layout_option')} | {s.get('window')} | "
                         f"{s.get('game_area')} | "
                         f"{s.get('vsync_option')}/{s.get('vsync_effective')}"
                         f"{' (PSX_VSYNC)' if s.get('vsync_env_override') else ''} | "
                         f"{s.get('world_percent')}% {s.get('world_requested')} | {s.get('world_actual')} | "
                         f"{reasons.get(s.get('world_reason'), s.get('world_reason'))}"
                         f"{' (max ' + str(s.get('max_texture')) + ')' if s.get('max_texture') else ''} |")
    lines += ["", "Hitches are guest frames over 25 ms, excluding native-movie playback and the "
              f"{report.CAPTURE_SETTLE_FRAMES} frames after each present capture. {report.GPU_SPAN_NOTE}"]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
