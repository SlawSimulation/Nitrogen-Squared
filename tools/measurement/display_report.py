"""GPL-3.0-or-later. Analysis for the original display measurement fixture.

Pure functions over the fixture's private CSV/JSON output: frame cadence,
CPU/GPU frame times, presentation pacing, hitch attribution, rendering memory,
PNG conversion and the test matrix. Nothing here reads game data; captures are
converted to PNG and stay under the caller's private .local output.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import struct
import zlib
from pathlib import Path

GUEST_HZ = 59.94                      # NTSC field rate the game is paced to
GUEST_PERIOD_MS = 1000.0 / GUEST_HZ   # 16.683 ms
HITCH_MS = 25.0                       # 1.5 guest periods
SEVERE_MS = 50.0
BYTES_COLOR = 4                       # GL_RGBA8
BYTES_DEPTH_STENCIL = 4               # GL_DEPTH24_STENCIL8 (driver-reported formats may pad)
SCRATCH_TILE = 2048                   # GL_SCRATCH_TILE in the pinned renderer
VRAM_W, VRAM_H = 1024, 512

TITLE_WIDTH, MENU_WIDTH = 640, 364     # legal/title card and game/menu scanout widths
WAIT_LIMIT = 9000                      # frames allowed for any awaited screen

# Active-low PS1 button words (route.py's encoding).
NONE, CROSS, TRIANGLE, START = 0xFFFF, 0xBFFF, 0xEFFF, 0xFFF7
CROSS_RIGHT, CROSS_LEFT = 0xBFDF, 0xBF7F

# Menu pages (the game's own menu-page word, read only by the fixture): 0 main
# menu, 3 ship select, 4 start level select; phase 29/30 are transitions. The
# menu ignores input during its fly-in, so each menu stage presses Cross every
# 120 frames until the page is left. Found with the "discover" route.
MAIN_MENU, SHIP_SELECT, LEVEL_SELECT = 0, 3, 4
PRESS_UNTIL_LEFT = [(CROSS, 20), (NONE, 100)]
# Gameplay frames are counted from the accepted level-1 start. The first help
# pop-up waits for input; Triangle answers "Disable help function".
START_STEPS = [(NONE, 250), (TRIANGLE, 20), (NONE, 60)]                     # 330 frames
# route.py's gameplay schedule, unchanged: thrust, steer, pause and resume.
GAMEPLAY_STEPS = [(CROSS, 240), (CROSS_RIGHT, 180), (CROSS_LEFT, 180), (CROSS, 400), (CROSS, 200),
                  (START, 20), (NONE, 180), (START, 20), (NONE, 180), (CROSS_RIGHT, 400),
                  (CROSS_LEFT, 500), (CROSS, 600)]                           # 3100 frames
LOOP_STEPS = [(CROSS, 500), (CROSS_RIGHT, 500), (CROSS, 100), (CROSS_LEFT, 500), (NONE, 20), (CROSS, 20)]

SCENES = [("level-1-loading-and-help", 0, 330), ("level-1-gameplay", 330, 1530), ("pause", 1530, 1750),
          ("level-1-gameplay-after-pause", 1750, 3450), ("extended-gameplay", 3450, 10**9)]
DEFAULT_CAPTURES = [0, 150, 300, 400, 700, 1100, 1500, 1600, 1800, 2200, 2700, 3200, 3400]
LONG_CAPTURES = DEFAULT_CAPTURES + list(range(4500, 16500, 750))
MATRIX_CAPTURES = [400, 1600, 2700]    # gameplay, pause, gameplay: enough to review framing and HUD
PRE_MENU_FRAMES = 6500                 # approximate movies, title, loading and menus at full speed
# Menu route for menu checks: walks the menus on fixed frames from the first
# menu scanout (the menu timing is deterministic from there; only the movies
# before it follow real time): attract title, main menu, ship select, level
# select, the level's first help pop-up accepted with Cross so its help pages
# show, then the next pages. (The legal card before it is presented once and
# then held, so it has no repeatable frame to capture.)
MENU_STEPS = [(NONE, 900), (CROSS, 20), (NONE, 500), (CROSS, 20), (NONE, 500), (CROSS, 20),
              (NONE, 900), (CROSS, 20), (NONE, 300), (CROSS, 20), (NONE, 300), (CROSS, 20), (NONE, 300)]
ROUTES = ("discover", "default", "long", "menus")


def stage(kind: str, a: int = 0, b: int = 0, delay: int = 0, timeout: int = WAIT_LIMIT, repeat: bool = False,
          steps=()) -> dict:
    return dict(kind={"display": 0, "page": 1, "now": 2}[kind], a=a, b=b, delay=delay, timeout=timeout,
                repeat=int(repeat), steps=list(steps))


def route_config(name: str) -> dict:
    """Condition-driven controller routes. Native movies follow real time, so
    the route waits for the title card, the main menu and each menu page rather
    than for fixed frame numbers; the last stage anchors on the accepted level
    start, and captures, samples and the stop point are relative to it."""
    if name not in ROUTES:
        raise ValueError(f"Unknown route {name}")
    if name == "menus":      # anchored on the first menu scanout after the title card
        return dict(stages=[stage("display", TITLE_WIDTH, delay=90, steps=[(CROSS, 20)]),
                            stage("display", MENU_WIDTH, steps=MENU_STEPS)],
                    stop_rel=sum(f for _, f in MENU_STEPS), captures_rel=[300], scale_rel=[])
    play = START_STEPS + GAMEPLAY_STEPS + (LOOP_STEPS * 8 if name == "long" else [])
    stages = [stage("display", TITLE_WIDTH, delay=90, steps=[(CROSS, 20)]),
              stage("display", MENU_WIDTH),
              stage("page", MAIN_MENU, delay=30, repeat=True, steps=PRESS_UNTIL_LEFT),
              stage("page", SHIP_SELECT, delay=30, repeat=True, steps=PRESS_UNTIL_LEFT),
              # Starting the level leaves the page word at 4 (the menu presenter
              # stops updating it), so press once after the page settles.
              stage("page", LEVEL_SELECT, delay=60, steps=[(CROSS, 20)]),
              stage("now", steps=play)]
    if name == "discover":   # maintenance aid: a capture every 30 frames
        return dict(stages=stages, stop_rel=3450, captures_rel=list(range(0, 3450, 30)), scale_rel=[600])
    if name == "default":
        return dict(stages=stages, stop_rel=3450, captures_rel=DEFAULT_CAPTURES, scale_rel=[600, 1100, 2700])
    return dict(stages=stages, stop_rel=16600, captures_rel=LONG_CAPTURES,
                scale_rel=[600, 1100, 2700, 9000, 15000])


def scene_of(rel: int | None, depth24: bool = False) -> str:
    """Scene for a frame relative to the level-1 start (None = before it)."""
    if depth24:
        return "movie"
    if rel is None:
        return "boot-title-loading-and-menus"
    for name, start, end in SCENES:
        if start <= rel < end:
            return name
    return "unlabelled"


def percentiles(values, points=(50, 95, 99)) -> dict | None:
    data = sorted(v for v in values if v is not None and math.isfinite(v))
    if not data:
        return None
    out = {}
    for p in points:
        k = (len(data) - 1) * p / 100
        lo, hi = math.floor(k), math.ceil(k)
        out[f"p{p}"] = round(data[lo] + (data[hi] - data[lo]) * (k - lo), 3)
    out.update(min=round(data[0], 3), max=round(data[-1], 3), mean=round(sum(data) / len(data), 3), n=len(data))
    return out


# ---- fixture output --------------------------------------------------------
def read_csv(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(newline="") as f:
        return [{k: int(v) for k, v in row.items()} for row in csv.DictReader(f)]


def read_info(path: Path) -> dict:
    """fixture-info.json is a list of one-key objects. Repeatable sample keys
    become lists; every other key keeps its first value."""
    repeated = ("scale_sample", "memory_sample", "window_sample", "guest_capture", "present_capture",
                "stage_anchor", "display_status")
    merged: dict = {k: [] for k in repeated}
    if not path.is_file():
        return merged
    for item in json.loads(path.read_text(encoding="utf-8")):
        for key, value in item.items():
            if key in repeated:
                merged[key].append(value)
            else:
                merged.setdefault(key, value)
    return merged


def busy_scale(timing: dict) -> float | None:
    """Nanoseconds per busy unit. Linux gives thread CPU ns. Windows gives
    thread cycles, converted with the invariant TSC rate measured over the run."""
    if timing.get("busy_unit") == "thread_cpu_ns":
        return 1.0
    ticks = timing.get("tsc_end", 0) - timing.get("tsc_start", 0)
    span = timing.get("end_ns", 0) - timing.get("start_ns", 0)
    if ticks <= 0 or span <= 0:
        return None
    return span / ticks


# ---- frame cadence and CPU/GPU frame times ---------------------------------
# Menu pages seen after level 1 on the long route (identified from captures).
PAGE_SCENES = {21: "level-1-bonus-tally", 11: "level-complete", 7: "next-level-select",
               23: "high-score-entry"}
ENTRY_FRAMES = 600                     # frames after a level-start press labelled as level entry


def label_scenes(rows: list[dict], anchor: int) -> list[str]:
    """One scene label per row. Before the level-1 start: boot, title, loading
    and menus. After it, the fixed schedule applies until the game's menu-page
    word changes; later menu pages are named from PAGE_SCENES, and the first
    Cross press on the next-level select starts a level-2 entry window."""
    labels, state, entry_until, last_page, buttons_prev = [], None, None, None, 0xFFFF
    for row in rows:
        frame, page = row["frame"], row.get("page")
        buttons = row.get("buttons", 0xFFFF)
        if row["depth24"]:
            labels.append("movie")
        elif not anchor or frame < anchor:
            labels.append("boot-title-loading-and-menus")
        else:
            if last_page is None:
                last_page = page
            phase = page
            settled = phase not in (29, 30)
            target = row.get("next_page") if phase == 30 else phase
            if settled and target != last_page or (phase == 30 and target != last_page):
                if target in PAGE_SCENES:
                    state = PAGE_SCENES[target]
                    last_page = target
            if state == "next-level-select" and not (buttons & 0x4000) and buttons_prev & 0x4000:
                state, entry_until = "level-2-entry", frame + ENTRY_FRAMES
            if state == "level-2-entry" and frame >= entry_until:
                state = "level-2-gameplay"
            labels.append(state or scene_of(frame - anchor))
        buttons_prev = buttons
    return labels


# Native movies play inside one guest frame while the host presents each movie
# frame; that many presents with an unchanged guest frame marks movie playback.
MOVIE_PRESENTS = 30
# A present capture (glReadPixels of the back buffer) runs at a swap and its
# cost lands in the next guest frames, after the frame row was flagged.
CAPTURE_SETTLE_FRAMES = 3
GPU_SPAN_NOTE = ("render_thread_gpu_span_ms is the framework's per-frame GL_TIME_ELAPSED query. Its bracket "
                 "includes the frame's swap, so it contains swap-chain and presentation waiting and is not "
                 "GPU render cost; hitches are therefore never attributed to the GPU from it.")


def present_counts(swaps: list[dict]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for s in swaps:
        counts[s["guest_frame"]] = counts.get(s["guest_frame"], 0) + 1
    return counts


def movie_frames(swaps: list[dict]) -> set[int]:
    """Guest frames during which the host presented a native movie."""
    return {f for f, n in present_counts(swaps).items() if n >= MOVIE_PRESENTS}


def capture_settle_frames(swaps: list[dict]) -> set[int]:
    """Guest frames whose timing a present capture can still disturb."""
    out: set[int] = set()
    for s in swaps:
        if s["instrumented"]:
            out.update(range(s["guest_frame"], s["guest_frame"] + CAPTURE_SETTLE_FRAMES + 1))
    return out


def frame_analysis(rows: list[dict], timing: dict, swaps: list[dict] | None = None) -> dict:
    scale = busy_scale(timing)
    anchor = timing.get("measure_anchor") or 0
    per_scene: dict[str, dict] = {}
    hitches = []
    last_origin = None
    last_flip_frame = None
    labels = label_scenes(rows, anchor)
    movies = movie_frames(swaps or [])
    settling = capture_settle_frames(swaps or [])
    for index, (prev, cur) in enumerate(zip(rows, rows[1:]), 1):
        frames = cur["frame"] - prev["frame"]
        if frames <= 0:
            continue
        # Swaps made while the guest was on frames prev..cur-1 fall in this interval.
        in_movie = any(f in movies for f in range(prev["frame"], cur["frame"]))
        scene = "native-movie" if in_movie else labels[index]
        s = per_scene.setdefault(scene, dict(frames=0, flips=0, wall=[], busy=[], rth_cpu=[], rth_gpu=[],
                                             flip_gaps={}, instrumented=0, settling=0, skipped_frames=0,
                                             span_ns=0))
        s["frames"] += frames
        s["span_ns"] += cur["t_ns"] - prev["t_ns"]
        if frames > 1:
            s["skipped_frames"] += frames - 1
        origin = (cur["display_x"], cur["display_y"])
        if last_origin is not None and origin != last_origin:
            s["flips"] += 1
            if last_flip_frame is not None:
                gap = cur["frame"] - last_flip_frame
                key = str(gap) if gap < 5 else "5+"
                s["flip_gaps"][key] = s["flip_gaps"].get(key, 0) + 1
            last_flip_frame = cur["frame"]
        last_origin = origin
        if in_movie:
            continue                      # host movie playback, not guest frame time
        if cur["instrumented"] or prev["instrumented"]:
            s["instrumented"] += 1
            continue                      # a sample/capture synchronized the renderer
        if cur["frame"] in settling:
            s["settling"] += 1
            continue                      # a present capture's cost is still landing
        wall = (cur["t_ns"] - prev["t_ns"]) / 1e6 / frames
        busy = (cur["busy"] - prev["busy"]) * scale / 1e6 / frames if scale else None
        s["wall"].append(wall)
        s["busy"].append(busy)
        rth = cur["rth_frames"] - prev["rth_frames"]
        cpu = gpu = None
        if rth > 0:
            cpu = (cur["rth_cpu_ns"] - prev["rth_cpu_ns"]) / 1e6 / rth
            s["rth_cpu"].append(cpu)
            g = cur["rth_gpu_frames"] - prev["rth_gpu_frames"]
            if g > 0:
                gpu = (cur["rth_gpu_ns"] - prev["rth_gpu_ns"]) / 1e6 / g
                s["rth_gpu"].append(gpu)
        if wall > HITCH_MS:
            hitches.append(dict(frame=cur["frame"], frame_from_level_start=cur["frame"] - anchor if anchor and cur["frame"] >= anchor else None,
                                scene=scene, wall_ms=round(wall, 3),
                                emulation_busy_ms=None if busy is None else round(busy, 3),
                                render_cpu_ms=None if cpu is None else round(cpu, 3),
                                render_gpu_span_ms=None if gpu is None else round(gpu, 3),
                                attribution=attribute(wall, busy, cpu)))
    scenes = {}
    any_gpu = any(s["rth_gpu"] for s in per_scene.values())
    for name, s in per_scene.items():
        seconds = s["span_ns"] / 1e9
        scene = dict(
            guest_frames=s["frames"], seconds=round(seconds, 3),
            guest_fps=round(s["frames"] / seconds, 3) if seconds else None,
            display_flips=s["flips"],
            game_frame_rate=round(s["flips"] / seconds, 3) if seconds else None,
            vblanks_between_flips=s["flip_gaps"],
            fixture_skipped_poll_frames=s["skipped_frames"],
            instrumented_intervals_excluded=s["instrumented"],
            after_present_capture_excluded=s["settling"])
        if name == "native-movie":
            scene["note"] = (f"guest frames during which the host presented {MOVIE_PRESENTS}+ times (native movie "
                             "playback); excluded from frame times and hitches")
        else:
            scene.update(
                wall_ms_per_guest_frame=percentiles(s["wall"]),
                emulation_thread_busy_ms=percentiles(s["busy"]) if scale else "unavailable: no thread-time source",
                render_thread_cpu_ms=percentiles(s["rth_cpu"]) or "unavailable: render worker off or not measuring",
                render_thread_gpu_span_ms=percentiles(s["rth_gpu"]) or (
                    "unavailable: no GPU timer results (driver lacks timer queries, or the render worker is off)"
                    if not any_gpu else "no GPU timer samples in this scene"))
        scenes[name] = scene
    return dict(level_start_frame=anchor or None, scenes=scenes, hitches=hitches, hitch_count=len(hitches),
                severe_hitches=sum(1 for h in hitches if h["wall_ms"] > SEVERE_MS),
                busy_conversion=("thread CPU ns" if scale == 1.0 else
                                 "thread cycles x TSC-calibrated ns/cycle (estimate)" if scale else "unavailable"),
                gpu_timer_note=GPU_SPAN_NOTE)


def attribute(wall: float, busy, render_cpu) -> str:
    """Name the likely cause of one long guest frame from what was measured.
    The GPU timer span includes swap waits (GPU_SPAN_NOTE), so it is not used."""
    budget = GUEST_PERIOD_MS
    if busy is not None and busy >= 0.8 * wall:
        return "emulation CPU (the game thread was busy for most of the frame)"
    if render_cpu is not None and render_cpu > budget:
        return "render-thread CPU (command replay, uploads or first-use work)"
    if busy is not None and busy < 0.5 * wall:
        return ("waiting (the game thread was mostly idle: GPU or swap-chain back-pressure, disc/file I/O, "
                "OS scheduling or pacing; not separable here)")
    return "mixed or unattributed"


# ---- presentation ----------------------------------------------------------
def refresh_multiples(intervals: list[float], refresh_hz: float) -> dict[str, int]:
    period = 1000.0 / refresh_hz
    multiples: dict[str, int] = {}
    for i in intervals:
        m = max(0, round(i / period))
        key = str(m) if m <= 4 else "5+"
        multiples[key] = multiples.get(key, 0) + 1
    return multiples


def presentation_analysis(swaps: list[dict], refresh_hz: float | None, present_kind: int,
                          scene_of_frame: dict[int, str] | None = None) -> dict:
    """Game presents (movie playback and present-capture aftermath excluded),
    with movie presents and per-scene spacing reported separately."""
    if not swaps:
        return dict(available=False, reason="no presents recorded")
    kind = {1: "OpenGL swap", 2: "SDL renderer present (software renderer)"}.get(present_kind, "unknown")
    movies = movie_frames(swaps)
    settling = capture_settle_frames(swaps)
    intervals, repeats, blocking, movie_intervals, excluded = [], 0, [], [], 0
    by_scene: dict[str, list[float]] = {}
    for prev, cur in zip(swaps, swaps[1:]):
        interval = (cur["after_ns"] - prev["after_ns"]) / 1e6
        if prev["guest_frame"] in movies and cur["guest_frame"] in movies:
            movie_intervals.append(interval)
            continue
        if (cur["instrumented"] or prev["instrumented"] or prev["guest_frame"] in movies
                or cur["guest_frame"] in movies or cur["guest_frame"] in settling):
            excluded += 1
            continue
        intervals.append(interval)
        blocking.append((cur["after_ns"] - cur["before_ns"]) / 1e6)
        if cur["guest_frame"] == prev["guest_frame"]:
            repeats += 1
        if scene_of_frame:
            by_scene.setdefault(scene_of_frame.get(cur["guest_frame"], "unlabelled"), []).append(interval)
    span = (swaps[-1]["after_ns"] - swaps[0]["after_ns"]) / 1e9
    result = dict(available=True, present_path=kind, presents=len(swaps),
                  presents_per_second=round((len(swaps) - 1) / span, 3) if span > 0 else None,
                  game_intervals=len(intervals), intervals_excluded=excluded,
                  interval_ms=percentiles(intervals, (1, 50, 95, 99)),
                  swap_call_ms=percentiles(blocking),
                  repeated_guest_frame_presents=repeats,
                  hitches_over_25ms=sum(1 for i in intervals if i > HITCH_MS),
                  hitches_over_50ms=sum(1 for i in intervals if i > SEVERE_MS))
    if intervals:
        mean = sum(intervals) / len(intervals)
        result["interval_stdev_ms"] = round(math.sqrt(sum((i - mean) ** 2 for i in intervals) / len(intervals)), 3)
        result["off_guest_period_over_2ms"] = round(
            sum(1 for i in intervals if abs(i - GUEST_PERIOD_MS) > 2.0) / len(intervals), 4)
    if refresh_hz and intervals:
        result.update(refresh_hz=refresh_hz, refresh_period_ms=round(1000.0 / refresh_hz, 3),
                      intervals_in_refresh_periods=refresh_multiples(intervals, refresh_hz),
                      expected_refreshes_per_guest_frame=round(refresh_hz / GUEST_HZ, 3))
    movie = dict(presents=sum(1 for s in swaps if s["guest_frame"] in movies),
                 interval_ms=percentiles(movie_intervals, (1, 50, 99)))
    if refresh_hz and movie_intervals:
        movie["intervals_in_refresh_periods"] = refresh_multiples(movie_intervals, refresh_hz)
    result["native_movie"] = movie
    if by_scene:
        result["by_scene"] = {name: dict(interval_ms=percentiles(values, (1, 50, 99)),
                                         hitches_over_25ms=sum(1 for i in values if i > HITCH_MS),
                                         **({"intervals_in_refresh_periods": refresh_multiples(values, refresh_hz)}
                                            if refresh_hz else {}))
                              for name, values in by_scene.items()}
    result["note"] = ("Times are when the swap call returned, not when the image was scanned out; "
                      "compositor and driver queueing are not visible here. Native-movie presents and the "
                      f"{CAPTURE_SETTLE_FRAMES} guest frames after each present capture are excluded from the "
                      "game figures.")
    return result


def scene_map(rows: list[dict], timing: dict, swaps: list[dict] | None = None) -> dict[int, str]:
    """Guest frame -> scene label, matching frame_analysis."""
    labels = label_scenes(rows, timing.get("measure_anchor") or 0)
    movies = movie_frames(swaps or [])
    return {r["frame"]: ("native-movie" if r["frame"] in movies else label) for r, label in zip(rows, labels)}


# ---- rendering memory -------------------------------------------------------
def surface_bytes(w: int, h: int, depth: bool = True) -> int:
    return w * h * (BYTES_COLOR + (BYTES_DEPTH_STENCIL if depth else 0))


def nominal_memory(scale: dict | None, world_raster: tuple[int, int] | None, max_dim: int | None = None) -> dict:
    """Bytes of the renderer's own large surfaces, from the pinned source:
    alloc_hr_targets() and Nitrogen's world raster (RGBA8 + DEPTH24_STENCIL8).
    Driver padding, the window's own back buffers and textures are excluded."""
    items = []
    if scale and scale.get("fbo", [0, 0])[0] > 0:
        w, h = scale["fbo"]
        s = max(1, int(scale.get("alloc_scale") or scale.get("effective") or 1))
        items.append(dict(name="hr VRAM surface", width=w, height=h, formats="RGBA8 + DEPTH24_STENCIL8",
                          buffers=2, bytes=surface_bytes(w, h),
                          formula=f"1024*{w // VRAM_W} x 512*{h // VRAM_H} px x (4 + 4) B"))
        sw, sh = (w, h) if s <= 2 else (min(w, SCRATCH_TILE), min(h, SCRATCH_TILE))
        items.append(dict(name="copy/stencil scratch (initial)", width=sw, height=sh, formats="RGBA8", buffers=1,
                          bytes=surface_bytes(sw, sh, depth=False),
                          formula="whole surface up to 2x, else one 2048 tile that grows on demand"))
        if scale.get("windowed"):
            items.append(dict(name="high-resolution window tiles", bytes=int(scale.get("window_mib", 0)) << 20,
                              formats="RGBA8 + DEPTH24_STENCIL8", buffers=2 * int(scale.get("window_tiles", 0)),
                              formula="reported by gl_renderer_scale_info"))
    if world_raster and world_raster[0] > 0 and world_raster[1] > 0:
        w, h = world_raster
        fits = max_dim is None or (w <= max_dim and h <= max_dim)
        items.append(dict(name="Nitrogen world raster", width=w, height=h, formats="RGBA8 + DEPTH24_STENCIL8",
                          buffers=2, bytes=surface_bytes(w, h) if fits else 0,
                          formula=f"{w} x {h} px x (4 + 4) B" + ("" if fits else
                                  f"; exceeds GL_MAX_TEXTURE_SIZE {max_dim}: not allocated, the world renders at 100%")))
    total = sum(i["bytes"] for i in items)
    return dict(items=items, total_bytes=total, total_mib=round(total / 2**20, 1), total_gb=round(total / 1e9, 3))


def measured_memory(samples: list[dict]) -> dict:
    usable = [s for s in samples if s.get("source") == "NVX"]
    if not usable:
        ati = [s for s in samples if s.get("source") == "ATI"]
        if ati:
            free = [s["texture_free_kib"] for s in ati]
            return dict(available=True, source="GL_ATI_meminfo", texture_free_mib_min=round(min(free) / 1024, 1))
        return dict(available=False, reason="the driver exposes neither GL_NVX_gpu_memory_info nor GL_ATI_meminfo")
    used = [(s["total_kib"] - s["available_kib"]) / 1024 for s in usable]
    return dict(available=True, source="GL_NVX_gpu_memory_info",
                dedicated_mib=round(usable[0]["dedicated_kib"] / 1024, 1),
                used_mib_first=round(used[0], 1), used_mib_peak=round(max(used), 1),
                evictions=max(s["evictions"] for s in usable),
                note="Used memory is for the whole GPU, including the desktop and other applications.")


# ---- images -----------------------------------------------------------------
def read_raw(path: Path) -> tuple[int, int, int, bytes]:
    data = path.read_bytes()
    magic, w, h, channels = struct.unpack_from("<4I", data)
    if magic != 0x5741524E or channels not in (3, 4) or len(data) != 16 + w * h * channels:
        raise ValueError(f"Not a fixture capture: {path.name}")
    return w, h, channels, data[16:]


def write_png(path: Path, w: int, h: int, channels: int, pixels: bytes, bottom_up: bool = False) -> str:
    """Write an 8-bit RGB/RGBA PNG with the standard library. Returns the SHA-256
    of the decoded pixels in top-down row order (the comparison identity)."""
    stride = w * channels
    rows = [pixels[y * stride:(y + 1) * stride] for y in range(h)]
    if bottom_up:
        rows.reverse()
    top_down = b"".join(rows)

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + r for r in rows)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6 if channels == 4 else 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
    path.write_bytes(png)
    return hashlib.sha256(top_down).hexdigest()


def compare_goldens(first: dict[str, str], second: dict[str, str]) -> dict:
    names = sorted(set(first) | set(second))
    same = [n for n in names if first.get(n) and first.get(n) == second.get(n)]
    return dict(compared=len(names), identical=len(same), differing=[n for n in names if n not in same],
                deterministic=bool(names) and len(same) == len(names))


GOLDEN_RUN = "win-1920x1080-100pct-vsync1-golden-a"


def compare_baseline(baseline: dict, runs: list[dict]) -> dict:
    """Compare this report's first golden run with the same run in an earlier
    report, capture by capture (guest scanout and presented frame). Identical
    hashes mean byte-identical decoded pixels."""
    def golden(rs):
        return next((r for r in rs if r.get("id", "").endswith("-golden-a") and r.get("status") == "completed"), None)
    old, new = golden(baseline.get("runs") or []), golden(runs)
    if not old or not new:
        return dict(identical_to_baseline=None, reason="both reports need a completed golden-a run")
    if old["id"] != new["id"]:
        return dict(identical_to_baseline=None,
                    reason=f"different golden runs ({old['id']} and {new['id']}); sizes must match")
    result = compare_goldens(old.get("captures") or {}, new.get("captures") or {})
    return dict(baseline_created=baseline.get("created"), baseline_build=(baseline.get("build") or {}).get(
                    "build_identity", {}).get("inputs_sha256"),
                compared=result["compared"], identical=result["identical"], differing=result["differing"],
                identical_to_baseline=result["deterministic"])


# ---- matrix -----------------------------------------------------------------
ASPECTS = [(0, "auto"), (1, "4x3"), (2, "16x9"), (3, "21x9"), (4, "32x9")]
WINDOWED = [(1440, 1080, "4:3"), (1920, 1080, "16:9"), (2520, 1080, "21:9"), (2560, 1440, "16:9"),
            (3840, 1080, "32:9"), (5120, 1440, "32:9"), (7680, 2160, "32:9")]
EXCLUSIVE = [(3840, 1080), (5120, 1440), (7680, 2160), (1920, 1080), (2560, 1440)]


def pick_display(displays: list[dict], index: int | None) -> dict | None:
    if not displays:
        return None
    if index is not None:
        return next((d for d in displays if d["index"] == index), None)
    return max(displays, key=lambda d: (d["bounds"][2], d["bounds"][3]))


def plan_runs(display: dict, preset: str) -> list[dict]:
    """The run list for one display. Windowed sizes larger than the display are
    marked oversized: rendered at that size, not fully visible. Exclusive modes
    are planned only when the display lists them."""
    bounds = display["bounds"]
    desktop = display.get("desktop") or {}
    modes = display.get("modes") or []

    def windowed(w, h, aspect, percent, vsync=1, route="default", tag=""):
        fits = w <= bounds[2] and h <= bounds[3]
        return dict(id=f"win-{w}x{h}-{percent}pct-vsync{vsync}{tag}", kind="windowed" if fits else "windowed-oversized",
                    width=w, height=h, aspect=aspect, render_percent=percent, vsync=vsync, route=route,
                    video_mode=0, mode="windowed", captures=MATRIX_CAPTURES)

    def exclusive(w, h, refresh, vsync=1, route="default"):
        return dict(id=f"excl-{w}x{h}@{refresh:g}-vsync{vsync}" + ("-long" if route == "long" else ""),
                    kind="exclusive", width=w, height=h, refresh=refresh, aspect=f"{w}:{h}",
                    render_percent=100, vsync=vsync, route=route, video_mode=0, mode="exclusive",
                    captures=MATRIX_CAPTURES if route == "default" else None)

    def listed(w, h):
        return sorted({round(m["refresh"], 3) for m in modes if m["w"] == w and m["h"] == h}, reverse=True)

    runs = []
    golden_size = (1920, 1080) if bounds[2] >= 1920 and bounds[3] >= 1080 else (bounds[2], bounds[3])
    if preset == "discover":
        run = windowed(*golden_size, "16:9", 100, 1, "discover", "-discover")
        run.update(golden=True, captures=None)   # the route's own dense captures
        return [run]
    for copy in ("a", "b"):
        g = windowed(*golden_size, "16:9", 100, 1, "default", f"-golden-{copy}")
        g.update(golden=True, captures=DEFAULT_CAPTURES)
        runs.append(g)
    if preset == "golden":
        return runs
    if preset == "v1":
        # Display V1 settings, applied through the player's options file (not
        # PSX_VSYNC), in the player's Borderless mode at the desktop size.
        dw, dh = desktop.get("w") or bounds[2], desktop.get("h") or bounds[3]
        for option, label in ASPECTS:
            runs.append(dict(id=f"v1-aspect-{label}", kind="borderless (V1 Aspect setting)", width=dw, height=dh,
                             aspect=label, aspect_option=option, render_percent=100, vsync=1, vsync_via="option",
                             route="default", video_mode=1, mode="none", captures=MATRIX_CAPTURES))
        runs.append(dict(id="v1-hud-centred", kind="borderless (V1.1 HUD position centred)", width=dw, height=dh,
                         aspect="auto", aspect_option=0, hud_layout=1, render_percent=100, vsync=1,
                         vsync_via="option", route="default", video_mode=1, mode="none", captures=MATRIX_CAPTURES))
        runs.append(dict(id="v1-vsync-off", kind="borderless (V1 VSync setting off)", width=dw, height=dh,
                         aspect="auto", aspect_option=0, render_percent=100, vsync=0, vsync_via="option",
                         route="default", video_mode=1, mode="none", captures=MATRIX_CAPTURES))
        clamp = windowed(7680, 2160, "32:9", 200)
        clamp.update(id="v1-clamp-7680x2160-200pct")
        runs.append(clamp)
        return runs
    dw, dh, dr = desktop.get("w"), desktop.get("h"), desktop.get("refresh")
    sizes = WINDOWED if preset == "full" else [s for s in WINDOWED if (s[0], s[1]) in ((3840, 1080), (5120, 1440))]
    for w, h, aspect in sizes:
        for percent in (100, 200):
            runs.append(windowed(w, h, aspect, percent))
    if dw and dh and dr:
        rates = listed(dw, dh)
        # The desktop rate, plus exact 2x/1x multiples of the game's cadence when listed.
        chosen = [r for r in rates if abs(r - dr) < 0.6 or
                  (preset == "full" and any(abs(r - hz) < 0.6 for hz in (120.0, 60.0)))]
        for r in chosen or [dr]:
            for vsync in (1, 0):
                runs.append(exclusive(dw, dh, r, vsync))
        # The player's own Window mode settings, applied by the game's options file.
        for video_mode, label in ((1, "borderless"), (2, "fullscreen")):
            runs.append(dict(id=f"{label}-desktop-vsync1", kind=f"{label} (player Window mode setting)",
                             width=dw, height=dh, aspect=f"{dw}:{dh}", render_percent=100, vsync=1,
                             route="default", video_mode=video_mode, mode="none", captures=MATRIX_CAPTURES))
    if preset == "full":
        for w, h in EXCLUSIVE:
            if (w, h) == (dw, dh):
                continue
            rates = listed(w, h)
            if rates:
                runs.append(exclusive(w, h, rates[0]))
    if preset in ("full", "stutter") and dw and dh and dr:
        for copy in (("a", "b", "c") if preset == "stutter" else ("a",)):
            run = exclusive(dw, dh, listed(dw, dh)[0] if listed(dw, dh) else dr, 1, "long")
            run["id"] += f"-{copy}"
            runs.append(run)
    if preset == "stutter":
        runs = [r for r in runs if r.get("route") == "long" or r.get("golden")]
    return runs


def estimate_minutes(runs: list[dict]) -> float:
    frames = sum(PRE_MENU_FRAMES + route_config(r["route"])["stop_rel"] for r in runs)
    return round(frames / GUEST_HZ / 60 + len(runs) * 0.5, 1)
