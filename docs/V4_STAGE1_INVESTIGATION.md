# V4 stage 1: picture enhancements and automated testing (investigation)

Investigation only, 2026-11-24, approved by the owner. No rendering behaviour,
setting, test infrastructure or installed software was changed. Each finding
is labelled with how it is known:

- **[code]** read in this repository or the pinned framework checkout
- **[measured]** observed on this server or in the owner's reports
- **[estimate]** reasoned, not yet measured
- **[untested]** cannot be confirmed without the proposed tooling

The pinned framework is `.local/psxrecomp` (unmodified). Nitrogen adapts
private copies of its renderer, GPU, GTE and main through
`tools/adapt_window_display.py`, `compat/world_raster.inc` and
`compat/window_gpu.inc`.

## 1. What already exists in the framework

| Capability | Where | Reachable in Nitrogen today? |
| --- | --- | --- |
| Perspective-correct textures and sub-pixel vertices (PGXP) | built-in mod `psx.enhancement.pgxp`; ENHANCEMENTS.md G1-G1.14 | No player control [code] |
| Scanlines (on, strength) | `gl_renderer_set_scanlines` | No |
| Screen colour model (CRT, Composite, Trinitron; phosphor-based LUT) | `gpu_set_screen_kind`, `color_lut.h`; mod `psx.enhancement.crt-filter` | No |
| Texture filtering (nearest, bilinear, xBR) | `gr_set_texture_filter`, per-primitive in `gpu_textured_triangle` | Forced to nearest every frame by `compat/runtime_host.cpp` (owner's 0.0.24 request) |
| Texture LOD and anisotropic filtering | `gl_renderer_set_texture_lod` | No |
| Dithering: original, off (true colour), per-pixel | `gl_renderer_set_dithering` | No (original) |
| Presentation gamma (brightness) | `gl_renderer_set_post_gamma` | No |
| Bloom (0-2), FXAA, user post-effect chains (`.pfx`, GLSL 330, up to 16 passes, mask/depth/history inputs) | `postfx_chain.h`, `docs/POST_EFFECTS.md` | No |
| HD texture packs (DuckStation PNG/JPEG/WebP format), texture dumping | mod `psx.enhancement.hd-textures`, `gpu_hd_textures.h` | No |
| Debug server: 376 commands incl. RAM read/write/dump, input routes, screenshots, perf, live renderer switches | `runtime/src/debug_server.c`, loopback only (127.0.0.1) | Built into the normal build; `run_runtime.py` passes `--debug-port 4370` |
| Save states | `savestate` command | **Refused**: loading is unavailable on LLE BIOS runs, which Nitrogen uses (`bios_backend=LLE` in the owner's log) |

Nitrogen's F1 menu (`compat/desktop_menu.cpp`) replaces the framework's
in-game menu and has no mod controls; the built-in mods are staged but no
player surface activates them.

## 2. Picture enhancement findings

### A. Perspective-correct textures

- Mechanism [code]: the GPU interpolates UV affinely; PGXP keeps the GTE's
  full-precision projection and, where it can prove which vertex the game drew,
  supplies a per-vertex weight so UVs are interpolated in perspective. Only UV
  interpolation inside a proven polygon changes; vertex positions do not, so
  this part cannot crack meshes (ENHANCEMENTS.md G1.8). Geometry correction
  (moving vertices) stays excluded.
- Coverage is the deciding question [code]: the framework records that on a
  base build Ridge Racer Type 4 got "0 of 282k textured triangles with
  perspective UVs"; useful coverage needs the hook build (`PSX_PGXP=1`,
  overlay flavor 2), where generated code carries `PGXP_*()` sites.
- Nitrogen builds the base flavor (`psxrecomp_add_game_runtime` without PGXP,
  CMakeLists.txt) [code]. Its native game slices are emitted with the overlay
  ABI, which "enforces the matching hook flavor" [code]. Switching to the hook
  flavor therefore changes the compiled game and the native slices, and would
  need native requalification against the accepted 38.4156% selection.
- Nitrogen's own path [code]: textured world polygons are drawn through
  `gpu_textured_triangle` into Nitrogen's world raster
  (`nitrogen_world_select`), the same function where the perspective weight is
  applied, so the mechanism is reachable. Whether the render worker's replay
  carries the weight, and the actual coverage on N2O's tunnel, are [untested].
- Performance [estimate]: the shadow tracking is CPU work on the emulation
  thread (the owner's PC has about 6 ms headroom of 16.7 ms); the shader cost is
  small.
- Recommendation: first measure coverage on the base build with the PGXP mod
  (debug server `pgxp` / `nclip_stats` counters). Ship only if coverage on the
  tunnel walls is high; otherwise a hook-flavor build is a separate, larger
  decision because it touches native qualification.

### B. CRT, scanlines and screen colour

- Scanlines [code]: a sinusoidal beam at the console's line pitch, in the
  present shader, faded in with output scale so it does not shimmer below 2x.
  It applies to the composed picture, including the HUD (period-accurate).
- Screen colour model [code]: present-time LUT with CRT (SMPTE-C phosphors,
  gamma about 2.4), Composite and Trinitron; raw is byte-identical.
- Not present [code]: curvature, shadow mask, phosphor persistence, film grain
  and vignette (searched; none in the renderer). "Phosphor glow" exists only as
  the colour model plus bloom; a dedicated glow needs a `.pfx` pass (V5/V7).
- Nitrogen interaction [code, untested visually]: Nitrogen composes its world
  raster into the high-resolution surface (`nitrogen_world_blit`) before the
  framework presents, so present-time effects see the final picture. Black
  bars come from the existing letterbox; whether scanlines are confined to the
  game rectangle and their pitch at 21:9/32:9 and odd window heights must be
  checked rendered.
- Performance [estimate]: one extra full-screen shader term; negligible.

### C. Texture smoothing (Sharp / Smooth)

- Mechanism [code]: the filter is chosen per primitive. Mode 2 (xBR) is
  limited to proven world geometry; mode 1 (bilinear) currently applies to every
  textured primitive, HUD included. Primitive UV bounds are clamped to the
  atlas rectangle ("inclusive atlas bounds"), which limits bleeding.
- Nitrogen already classifies its HUD and menu primitives
  (`nitrogen_gpu_ui_flags`) immediately before the filter is chosen, so a small
  adapter change can keep UI sharp while world polygons are smoothed [code].
  The current global forcing in `runtime_host.cpp` would become an option.
- Risks [untested]: seams where N2O packs several textures into one page,
  colour-key (cutout) edges on sprites and particles, and the PS1's
  semi-transparency. Bilinear on palette (CLUT) textures is done after lookup in
  the shader [code].
- Independence [code]: filtering is chosen per primitive in the rasteriser;
  scanlines and colour model act at present time. They do not interact.
- Performance [estimate]: up to four texture taps per world pixel; small on the
  owner's GPU, significant under software rendering.

### Future HD texture packs

The framework's HD pack system already loads DuckStation-format packs on
OpenGL and can dump original textures [code]. V4 decisions that keep it open:
make filtering a per-primitive class decision (UI sharp, world chosen), not a
global switch; keep perspective UVs (they matter most for replaced high-res
wall art); do not bake effects into textures. Packs are derived art: they must
live in the player's private folder and never in Git or releases. Untested with
Nitrogen's world raster.

## 3. Proposed V4 stage 2 (needs approval)

Recommended, in order:

1. Texture filtering: Sharp (default) / Smooth (world only, UI sharp).
2. Scanlines with strength, and screen colour model (Original / CRT /
   Composite / Trinitron).
3. Brightness (presentation gamma) and Dithering (Original / Off / Fine).
4. Perspective-correct textures, only if the coverage measurement passes.

Deferred: phosphor glow, curvature, bloom, FXAA, anisotropic filtering
(V5/V7, mostly through the existing post-effect chain).

### F1 Picture section (proposal)

```
Picture
  Preset            [Original PS1 v]  [Reset to original]
  -- Textures --
  Texture filtering [Sharp v]          (Sharp, Smooth)
  Perspective-correct textures [ ]     (hidden until qualified)
  Dithering         [Original v]       (Original, Off, Fine)
  -- Screen --
  Colour model      [Original v]       (Original, CRT, Composite, Trinitron)
  Scanlines         [x] strength [====|----] 50%
  Brightness        [====|----] 100%
```

Presets only write the individual settings; changing any setting afterwards
shows the preset as "Custom". Reset writes all defaults (the original look).

### Settings (`nitrogen-options.ini`, all default = original)

| Key | Values | Default |
| --- | --- | --- |
| `texture_filter` | 0 sharp, 1 smooth | 0 |
| `perspective_textures` | 0, 1 | 0 |
| `dithering` | 1 original, 0 off, 2 fine | 1 |
| `screen_model` | 0 original, 1 CRT, 2 composite, 3 Trinitron | 0 |
| `scanlines` | 0 off, 10-100 strength % | 0 |
| `brightness` | 80-130 % (gamma) | 100 |

Nitrogen owns these through its own options, as for Video; the framework mods
stay inactive so two places never own the same state. All are applied live.

## 4. Automated testing findings

### What exists today [code, measured]

- 264 source tests (unit and compiled fixtures), source audit, and three CI
  workflows. CI Linux already renders through Mesa with
  `SDL_VIDEODRIVER=offscreen` and `EGL_PLATFORM=surfaceless`, but only synthetic
  programs: the game disc must never reach GitHub.
- `tools/measurement`: a fixture linked against the real build that drives
  controller routes anchored on game state, captures guest and presented
  images, records per-frame timing, RAM dumps, F1-equivalent requests and the
  projection squeeze; determinism measured (26/26 identical golden captures).
- `tools/qualification/route.py`: long routes, combat inputs, JSON controller
  scripts and disposable copied memory cards.
- The debug server (above). Save states cannot reset tests on LLE runs.

### Software OpenGL (recommended: Mesa llvmpipe, app-local)

- Server [measured]: Intel Xeon E5-2673 v4, 20 cores / 40 threads, 16 GB RAM,
  virtual machine, no GPU exposed (Microsoft Remote Display and Basic Display
  adapters only), OpenGL 1.1.
- Mesa 26.3.1 (pal1000 mesa-dist-win release-mingw, published 2026-11-23,
  SHA-256 `c7d8c39f…` matching GitHub's digest) provides llvmpipe with OpenGL
  4.5 core [code: release metadata; untested on this server].
- Isolation [measured]: `opengl32` is not in Windows' KnownDLLs, so a private
  `opengl32.dll` + `libgallium_wgl.dll` placed next to a test executable is used
  by that executable only. No system driver, registry or System32 change.
  Never placed next to the player executable.
- Extraction needs 7-Zip's `7zr.exe` 26.06 (official ip7z release, GitHub
  digest `e80db2da…`); Python extractors lack the BCJ2 filter. Running it was
  blocked pending the owner's permission.
- Headless [estimate]: llvmpipe needs no display; tests run in the logged-in
  session (disconnecting RDP keeps it). WSL2 is not suitable: the server is a
  VM and it would need system changes and a reboot.
- Performance [estimate]: `LP_NUM_THREADS` uses the cores, but 32:9 at 100%
  draws a 11264x5632 surface plus a 10803x2304 world raster per frame; expect
  single-digit frames per second there. Routes are anchored on game state, so
  slow rendering lowers throughput, not correctness; movie-timed scenes and
  timing measurements are not representative and stay hardware-only.

### Proposed test system (`n2test`)

A scenario runner over the existing fixture and route tools:

- Scenario files (JSON): window size and mode, settings, route or memory-card
  start, capture frames, assertions (page reached by frame N, RAM values,
  region image comparisons, frame-time budgets).
- Image comparison: per-region masks (world, HUD, bars), tolerance by mean and
  maximum channel difference and changed-pixel ratio, plus exact hashes where
  determinism is proven (all-enhancements-off must equal the baseline exactly).
- Baselines kept privately in `.local` (game-derived images never enter Git);
  separate baseline sets for software-rendered and hardware-rendered results.
- Reports (JSON and Markdown) labelling each result: source-verified,
  unit-tested, integration-tested, software-rendered, hardware-rendered,
  gameplay-tested, hardware-and-display verified, not tested.
- Test hooks: the measurement fixture is a separate executable, so the player
  build is unchanged; no hooks are added to normal releases.

### Where each test runs

| Layer | Runs on | Real game? |
| --- | --- | --- |
| Source, unit, synthetic renderer | GitHub-hosted CI (Windows, Linux) | No |
| Gameplay and regression routes, software-rendered images | this server | Yes (private import) |
| Display modes, refresh, real GPU performance | owner's PC, optional standalone kit (`measure_display.py`) | Yes |

No self-hosted runner, remote access or scheduled job on the owner's main PC;
CI never depends on it.

### Limits (cannot be automated here)

- Physical display behaviour: exclusive mode switching, 59.97/144 Hz cadence,
  judder perception, monitor scaling, VRR.
- Real GPU performance and driver-specific defects (NVIDIA path).
- Two-controller split screen until the route tool can drive player 2 and the
  menu's step size is controlled (observed unreliable).
- Audio quality judgement (levels and continuity can be measured).
- Levels beyond those reachable by routes or saved cards; no save-state resets.

## 5. Proposed implementation order

| Step | Content | Complexity | Depends on |
| --- | --- | --- | --- |
| T1 | Install Mesa llvmpipe app-locally; prove the real game renders (menus, level 1, 4 aspect sizes) | low | approval to run 7zr.exe |
| T2 | `n2test` scenario runner, region comparison, reports; software baselines for today's look | medium | T1 |
| G0 | PGXP coverage measurement on the base build | low | T1 |
| V4-a | Texture filtering (world/UI split) + Picture section skeleton + settings | medium | T2 for automatic checks |
| V4-b | Scanlines, colour model, brightness, dithering, presets, reset | low-medium | V4-a |
| V4-c | Perspective textures, only if G0 passes | medium (high if hook flavor) | G0 |
| T3 | Gameplay regression scenarios (menus, levels via cards, extras, video options) | medium | T2 |
| T4 | CI: keep synthetic; add renderer unit fixtures for new options | low | V4-a |

Owner involvement: approve T1 (software installation) and the V4 stage 2
feature list; an optional final look on the real GPU at the end of V4.
