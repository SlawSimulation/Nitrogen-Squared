# Display validation

## V0 display baseline and measurement tooling (feature branch, unreleased) — 2026-11-23

V0 adds measurement tooling only. The game, its rendering, timing, assets,
VERSION and the compiled-game fingerprint are unchanged; the player executable
is never rebuilt or modified. Coverage remains 38.4156% linked / 61.5844% outside.

### Tooling

`tools/measurement/measure_display.py` links `display_fixture.cpp` separately
against the existing normal (diagnostics-off) game build, as the qualification
fixture does, with `--wrap` interposition of `SDL_PollEvent`,
`SDL_GL_SwapWindow` and `SDL_RenderPresent`. It rebuilds Ninja's temporary
link response file from `build.ninja`, runs only the compiler-driver link into
a private folder and checks the player executable's SHA-256 before and after.
Each run uses its own options file, memory cards, menu preferences (F1 bar
hidden, so the game area equals the window) and copies of the BIOS/assets; the
player's bindings, mods and saves are not used. Output stays under
`<install>/.local/measurements/display-<time>/`.

Input goes through the framework's offline controller source
(`psx_mod_set_controller_source`), the per-frame pad path its playtest replay
uses. An SDL virtual gamepad was tried first and rejected: presses registered
inconsistently and two identical runs diverged. Native movies follow the audio
clock and the main menu ignores input during its fly-in, so absolute frame
schedules only fit one host speed. The route therefore waits for observed
states: the 640-wide title card, the 364-wide main menu, then the game's own
menu-page word (read only, the address `native/options_entry.cpp` already
uses: 0 main menu, 3 ship select, 4 start level select, 29/30 transitions).
Gameplay is counted from the accepted level-1 start: Triangle disables the help
pop-ups, then route.py's thrust/steer/pause schedule runs unchanged. The long
route continues through level-1 completion, the bonus tally (page 21), level
complete (11), next-level select (7), level-2 entry and gameplay, ending at
high-score entry (23).

Measured per run: guest frames and display flips (display-origin changes per
VBlank) by scene; emulation-thread busy time (thread CPU time on Linux, thread
cycles converted with the run's measured TSC rate on Windows); render-thread
CPU and GPU time from the framework's own per-frame timer queries; swap-call
return times, their spacing in refresh periods, repeated guest frames and
hitches; `gl_renderer_scale_info` samples; driver GPU memory
(`GL_NVX_gpu_memory_info` or `GL_ATI_meminfo`); window, display mode, HUD step
and world-raster size; and PNG captures of the guest scanout (golden and long
runs) and of the presented frame. Hitches over 25 ms are attributed to
emulation CPU, render-thread CPU, GPU or waiting from those measurements.
Unavailable values are reported as unavailable, never as zero. The report also
lists every Windows display, its EDID name, preferred (native) mode and every
listed mode, flagging modes above native as likely GPU-scaled (for example
NVIDIA DSR). Windowed sizes larger than the display are `windowed-oversized`:
rendered at that size but not fully visible, never counted as a tested display
mode. Exclusive modes are tested only when the display lists them; the player's
own Borderless and Fullscreen settings are measured separately.

### Local tooling validation (this host)

This Windows Server host exposes only OpenGL 1.1 and a 1280x720 32 Hz remote
display, so it validates the tooling with the software renderer. Its timings
are not representative and are not baselines.

| Check | Result |
| --- | --- |
| Link against the real normal build; player executable SHA-256 unchanged before/after every run | pass |
| Probe: displays, modes, window state, timing, present path recorded | pass |
| Golden determinism, two default-route runs: 13 of 13 guest captures identical; level 1 started on the same frame | pass |
| Long route: level-1 completion, bonus, level complete, next-level select, level-2 entry and gameplay, high-score entry; captures confirm each screen | pass |
| Exclusive 1280x720@32 requested and recorded | pass |
| 20 synthetic tests (routes, scene labels, cadence, hitch attribution, 144 Hz pacing, memory formula, PNG, plans, link and Ninja parsing, fixture reads guest RAM only) | pass |
| OpenGL present capture, GPU timer and NVX memory paths | not exercised here (no OpenGL 3); first run is on the owner's PC |

Cadence (game logic, independent of renderer and host speed): N2O flips its
display on every VBlank in menus, level loading, gameplay and the pause screen,
a 59.94 Hz game. The opening movies flip every two or three VBlanks (about
24 fps).

### Rendering memory estimate

From `alloc_hr_targets()` in the pinned renderer: one GL_RGBA8 colour texture
(4 B/px) and one GL_DEPTH24_STENCIL8 renderbuffer (4 B/px) at 1024·S × 512·S,
where Nitrogen requests S = max(⌈height/240⌉, ⌈viewport width/364⌉). At
5120x1440, S = 15: 15360 × 7680 = 117,964,800 px × 8 B = 943,718,400 B
(0.94 GB, 900 MiB), confirming the earlier estimate. Above 2x the copy scratch
starts at one 2048 tile. Nitrogen's world raster is a separate RGBA8 +
DEPTH24_STENCIL8 surface allocated at every percentage, so the total is roughly
both columns. Calculated (source height 240), to be verified on hardware:

| Window | S | VRAM surface | MiB | World 100% | MiB | World 200% | MiB |
| --- | ---: | --- | ---: | --- | ---: | --- | ---: |
| 1440x1080 | 5 | 5120x2560 | 100 | 4051x2304 | 71 | 8102x4608 | 285 |
| 1920x1080 | 6 | 6144x3072 | 144 | 5402x2304 | 95 | 10803x4608 | 380 |
| 2520x1080 | 7 | 7168x3584 | 196 | 7090x2304 | 125 | 14179x4608 | 498 |
| 2560x1440 | 8 | 8192x4096 | 256 | 7202x3072 | 169 | 14404x6144 | 675 |
| 3840x1080 | 11 | 11264x5632 | 484 | 10803x2304 | 190 | 21606x4608 | 760 |
| 5120x1440 | 15 | 15360x7680 | 900 | 14404x3072 | 338 | 28808x6144 | 1350 |
| 7680x2160 | 22 | 22528x11264 | 1936 | 21606x4608 | 760 | 43211x9216 | 3038 |

A world raster wider than GL_MAX_TEXTURE_SIZE is not allocated and the world
silently renders at 100%; a VRAM surface beyond the limit or the 2 GiB budget
switches the framework to its high-resolution window. Both thresholds depend
on the GPU, which the report records.

### Risks found during V0

- Native movies and menu input timing follow real time: absolute frame
  schedules are only valid at one host speed (solved by state-anchored routes).
- The internal scale follows window width: ultrawide windows allocate very
  large square-scaled surfaces (table above).
- 200% world rendering silently falls back above the texture limit, and Settings
  does not report it (V1 clamping and reporting).
- 59.94 Hz on 144 Hz: the framework keeps a swap interval of 1 because 144 Hz
  exceeds the game rate, but VSync does not own the cadence unless the refresh
  matches within 2%. The pacer then presents on the next refresh, so frames are
  expected to alternate between 2 and 3 refreshes (13.9 / 20.8 ms, 2.4 average).
  VSync off should give even spacing with tearing; 120 Hz is an exact 2x
  multiple. To be measured.
- Present times are swap-call returns, not scanout; compositor queueing is not
  visible.
- Fixture-selected exclusive modes use SDL directly; the player's Fullscreen
  setting uses the framework helper at the desktop mode and is measured as its
  own run.

### Level-entry stutter

Still OPEN. The long route crosses two level entries: level 1 (0-330 frames
after the start press) and level 2 (600 frames from the Cross that starts it),
labelled as their own scenes, with every hitch attributed. The `stutter` preset
runs the long route three times. This host cannot reproduce Windows timing, so
no cause is claimed yet.

### Pending

Owner measurements on the GTX 1080 Ti and Samsung 49-inch ultrawide (desktop
3840x1080 at 144 Hz): `quick` or `full`, then `stutter`. V1 does not start
until those findings are documented and reviewed.
