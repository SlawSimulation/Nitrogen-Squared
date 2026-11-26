# Display validation

## V4 Picture settings and automated testing (dev, 0.2.2, unreleased) — 2026-11-25

Owner-approved V4-a and V4-b, with the HUD-mask testing follow-up. V4-c
(perspective-correct textures) is not implemented. VERSION stays 0.2.2;
compiled-game inputs change (one rebuild). Native selection, coverage
(38.4156% / 61.5844%), clocks and the framework checkout are unchanged.

### Settings

`texture_filter` (0 Sharp, 1 Smooth), `dithering` (0 Original, 1 Off, 2 Fine),
`screen_model` (0 Original, 1 CRT, 2 Composite, 3 Trinitron), `scanlines`
(0 or 10-100 in steps of 10), `brightness` (80-130 in steps of 5, 100 =
unchanged). All default to the original look and are applied live only when
they change; Original restores the framework's own startup value. Presets
(Original PS1, Smooth, CRT, Composite TV) only write these settings; any
further change shows Custom. Reset to original applies the Original preset.

- Smooth: Nitrogen stops forcing the framework's filter off every frame. In
  the adapted renderer, primitives Nitrogen tags as HUD/menu and 2D sprite
  rectangles keep nearest sampling; the framework's own "world only" rule
  needs PGXP-proven geometry, so Nitrogen's tagging is used instead.
- Dithering, scanlines and brightness use the framework's public setters.
- Colour models: the framework's screen-colour LUT is applied only where the
  CPU converts 15-bit scanout, which Nitrogen's OpenGL presentation never
  reaches; the first Mesa run showed CRT, Composite and Trinitron changed no
  pixel. They now run as a one-pass post effect through the framework's live
  chain with the framework's own constants (color_lut.c). A unit test compares
  the formula with the framework LUT for all 32,768 15-bit colours of each
  model: within 3 of 255 levels and at most 0.5 on average (the GPU sees the
  presented 8-bit value, the LUT the 5-bit index).

### Test infrastructure

- Mesa 26.3.1 llvmpipe (checksum-verified) placed only beside private test
  executables; OpenGL 4.6 Core; deterministic: repeated runs are byte-identical.
- `tools/measurement/n2test.py` runs real-game scenarios (27 files) and labels
  evidence; captures and baselines are private in `.local`. Routes: default and
  long gameplay, and `menus` (anchored on the first menu scanout after the
  legal/title card: attract title, main menu, ship and level selection, three
  help pop-ups, level caption). The legal/title card is shown once with no
  repeatable frame, so it has no route. Scenarios can open the F1 menu at a
  frame (`menu_open_at`), and image regions can compare content that moved by
  a stated `offset`.
- HUD comparison (`hud_sharp`). A test-only switch in the adapted renderer
  collapses either the primitives Nitrogen tags as HUD/menu (bit 0) or every
  other primitive (bit 1) off screen; every VRAM sync and batch step still runs
  (an early return first skipped the VRAM sync and changed later world texels
  by one colour step). It is always off in normal play. The layer is hidden
  only in the drawn frame (two frame hooks before presentation, measured).
  All HUD runs use the render worker OFF: with it on, captures at frames 400
  and 1600 show the game frame one step earlier than worker-off captures do,
  so the first masks (worker-off) were misaligned with the worker-on images.
  That misalignment, plus translucent glass, caused the earlier wrong
  conclusion that Smooth blurred the weapon model.
  For a reference scenario, `--make-masks` records four private files per
  capture from four worker-off runs: the composite (`hudref`), the HUD alone
  over a frame cleared to black (`hudlayer`; the game never clears its draw
  buffer, so the world-hidden frame clears the draw area first, otherwise it
  would still hold the world from two frames earlier), the same HUD layer over
  a frame cleared to white (`hudwhite`, a second test-only bit) and the mask
  (where the composite and a HUD-hidden run differ, grown by one pixel). The
  check then:
  1. requires the HUD layer to be byte-identical over the whole frame;
  2. requires proven opaque HUD pixels to be byte-identical in the composite.
     Opacity is proven, not inferred: inside the mask, the reference HUD layer
     is identical over black and over white, and the reference composite equals
     it (nothing drawn over the HUD). Every PS1 blend mode is monotone in the
     background colour, so a pixel equal over both extremes is the same over any
     world;
  3. reports, without gating, translucent pixels (everything else in the mask:
     the world shows through or draws over the HUD).
  This replaces the earlier inference (composite equal to the black-background
  layer and different from the world). On the menus it misclassified dark
  translucent panels as opaque: where the world behind rounds to black, one
  reference frame cannot show the translucency, and Smooth then changed those
  "opaque" pixels. The white background shows it, and the separate
  "ambiguous" class is no longer needed. The new rule only removes pixels the
  reference cannot prove opaque; the HUD-layer check is unchanged. Re-recording
  with the rebuilt binary reproduced every existing `hudref`, `hudlayer` and
  mask file of the five reference scenarios byte-for-byte.
  "HUD" means primitives Nitrogen tags as HUD/menu. A few untagged effect
  quads drawn above the HUD (mid-screen pause effects) follow the world filter;
  untagged 2D rectangles keep nearest sampling anyway.

### Results (software-rendered, Mesa llvmpipe; not GPU-representative)

| Check | Result |
| --- | --- |
| Original look on the final build, all settings at default: software guest image and Mesa at 1024x768, 1920x1080, 2560x1080, 3840x1080 | 5/5 byte-identical to the pre-V4 baselines (15 images) |
| Each setting changes the picture (1024x768, frames 400/1600/2700): Smooth, dithering Off/Fine, CRT, Composite, Trinitron, scanlines 50%, brightness 120% | pass; e.g. CRT 99.7% of pixels, mean 10.6 levels; brightness mean 15 levels |
| Combinations at 21:9 (Smooth + CRT + scanlines) and 32:9 (Smooth, dithering off, Composite, scanlines 30%, brightness 105%) | pass |
| Reset to original mid-game | byte-identical to the original at every later capture; original values saved |
| Persistence | a mid-game F1-equivalent change is saved and a real restart from that file renders byte-identically to the same settings set before launch |
| Determinism | every V4 scenario re-run matches its recorded baseline exactly |
| Smooth keeps the gameplay HUD identical, 1024x768 (frames 400/1600/2700, against the original) | pass. HUD layer byte-identical over the whole frame at all three. Proven opaque HUD pixels byte-identical: 17,548 / 19,723 / 18,118 (71.9% / 75.2% / 74.4% of the mask). Translucent (reported): 1,570 of 6,860 / 2,296 of 6,497 / 1,721 of 6,250 changed, max 32 / 192 / 32 levels, from the smoothed world behind the glass |
| Same at 1920x1080 (16:9), added after the regression below | pass. HUD layer byte-identical; opaque 70,192 / 78,926 / 72,202 exact (78.2% / 82.2% / 81.0%); translucent 3,988 of 19,624 / 4,024 of 17,102 / 4,489 of 16,888 changed, max 32 / 120 / 40. Recording its HUD references left the existing original 1920x1080 captures byte-identical (checked by hash), the new Smooth baseline is new rather than a replacement, and both scenarios then pass against their baselines |
| Same at 2560x1080 (21:9) | pass. HUD layer byte-identical; opaque 70,192 / 78,925 / 72,209 exact (78.2% / 82.2% / 81.0%); translucent 4,760 of 19,624 / 4,139 of 17,102 / 4,833 of 16,895 changed, max 32 / 149 / 40 |
| Same at 3840x1080 (32:9) | pass. HUD layer byte-identical; opaque 70,192 / 78,928 / 72,256 exact (78.2% / 82.2% / 81.0%); translucent 5,154 of 19,624 / 5,471 of 17,101 / 7,164 of 16,910 changed, max 32 / 51 / 40. Because the HUD layer is byte-identical, the HUD sits exactly where Original puts it at that size; the world changed in about 30% of pixels |
| Smooth keeps the menu HUD identical (menus route, 8 screens: attract title, main menu, ship and level selection, three help pop-ups, level caption) | pass at 1024x768 and 2560x1080. HUD layer byte-identical at all 16 captures. Opaque exact: 2,544 / 15,671 / 12,192 / 19,600 / 37,612 / 17,588 / 42,552 / 27,449 at 1024x768 and 10,176 / 62,710 / 48,768 / 78,400 / 150,448 / 70,352 / 170,208 / 109,798 at 2560x1080. The menus are mostly translucent panels over moving backdrops (for example 34,143 of 177,141 translucent pixels changed on a help screen). Untagged by Nitrogen, and therefore filtered like the world: the attract logo and frame, the main-menu rings and the level caption |
| F1 settings (Picture section) with Smooth | pass. The F1 overlay is drawn after presentation and effects, so the texture filter never reaches it. Above the Picture values and outside the panel it is byte-identical to the original (563,200 + 122,080 + 4,818 pixels). Smooth adds one help line under Textures, so the rest of the panel moves down 19 px; compared at that offset it is byte-identical too (64,525 pixels). Only the Preset/Textures values and the scrollbar thumb differ, as designed |
| Finding: top row of the attract capture with the render worker on | at frame 150 of the menus route (1024x768), row 0 of the worker-on capture is sometimes black and sometimes holds picture content, with the original settings as well as Smooth (original: black in 3 runs, content in 1; Smooth: black in 1, content in 3). Each run that differs from its baseline fails the whole-frame self-check by exactly that row (1,024 pixels); in the final re-run the Smooth scenario matched. Worker-off captures of the same frame are stable in every run, and the HUD checks there pass. It is render-worker timing in the capture, not a Smooth effect (an earlier note blaming bilinear sampling was wrong). The check is kept and reported as failing; it is not loosened |
| Host display change during the regression | the server's remote-desktop display dropped to 1280x720 during the full run. The software-renderer scenario then got a 1020x536 window, one HUD reference run changed size mid-run, and the F1 panel layout moved; all were reported as failures (size mismatch), none passed at the wrong size. Re-run afterwards, all three pass, so all 26 scenarios then in the suite have passed on the final build; the 27th (Smooth at 1920x1080) passes as shown above |
| Weapon model under Smooth | sharp: it is drawn at ranks 427/428 inside the gauge and already tagged HUD, so it keeps nearest sampling; it is byte-identical in the HUD layer. The earlier note that it was smoothed was wrong (misaligned worker-on/off masks plus translucent glass); no rendering change was made |
| HUD reference reproducibility | re-recording the five original HUD references (gameplay at three sizes, menus at two) after the rebuild reproduced every existing composite, HUD layer and mask file byte-for-byte; only the white layers were added. (Normal worker-on captures are not re-recorded by this step; they are checked by the regression run) |
| Dithering and the HUD | the PS1 dithers HUD primitives too, so those scenarios compare the world only |
| Unit tests | 306 run, 0 skipped (C++ compiler available); only the 7 pre-existing test_portable git-fixture errors (also present without these changes; left untouched); source audit passes (247 files). Re-run after adding the 1920x1080 scenario and the owner checklist: same result, 306 run, the same 7 errors only; audit passes (248 files); 27 scenarios valid |

Not tested here: the owner's GPU and monitor (real colour, scanline moiré at
32:9, performance). Software-rendered timings are not meaningful. Optional
owner check: N2-DISPLAY-V4.

## V3 stage 1: ultrawide investigation (dev, 0.2.2, no runtime change) — 2026-11-24

Investigation only, approved by the owner; no widening is implemented. The
game build, VERSION, native selection, coverage and clocks are unchanged
(compiled-game inputs stay `35885323…`). The measurement fixture gains a
maintenance key, `gte_aspect N D`, which applies the guest-visible half of
the widescreen path (the framework's `gte_set_display_aspect` projection
squeeze) independently of the renderer. All output stays in `.local`.

### How wide views work today

At a wide game area the host calls `gte_set_display_aspect`: the GTE squeezes
X so the wider field projects into the game's original viewport (mode 1), and
the presentation layer stretches it back. The game's own polygon clipping
therefore already receives the full width. The squeeze is visible to the
game's code, so gameplay effects were checked directly.

### Static scan

The framework's own `psxrecomp-analyze --scan-widescreen` (built privately,
unchanged checkout) on the verified N2O.EXE found 80 GTE functions, 3 with the
screen-extent signature (already covered by the framework's automatic mode),
no bias/range X-window cull pairs, and 185 low-confidence (40-65%) width
compares, mostly outside GTE code. N2O shows no classic screen-X cull window.

### Real-game runs (software renderer, identical controller route)

| Check | 4:3 | 21:9 | 32:9 |
| --- | --- | --- | --- |
| Route outcome | level 1 completed; bonus tally at frame 7689; level complete at 7823 | identical frames | identical frames |
| Score and HUD values at sampled frames | reference | identical | identical |
| Guest RAM at four gameplay checkpoints | reference | ~91-96 KB differ | ~94-101 KB differ |
| Emulation-thread work, median gameplay frame | 1.000 | 0.998 | 0.990 |

The RAM differences are all screen-space and drawing data: the two GPU packet
buffers (about 58 KB each), the ordering tables (about 1.5 KB), projected
screen X values in object and point tables (squeezed toward the centre; for
example an object at X -92, off screen in 4:3, is at 80 in 32:9), packet
pointers and primitive counts. No progression, score or timing value differs.
This shows the current wide projection does not change gameplay on this route;
it does not prove every level or event.

Edges: 44 gameplay frames at 32:9 (every 30 frames) and two dense windows (every
2 frames, frames 1962-2000 and 2350-2418) were inspected with the original 4:3
boundary marked. Objects are drawn in the extra side areas and are followed to
the real screen edge on both sides; none disappears at the old 4:3 boundary,
and none was seen popping in at the sides (objects arrive from the tunnel's
vanishing point). The darkest outer zone found (frame 1740) matches the unlit
tunnel visible in the 4:3 frame itself, not missing walls.

Non-gameplay screens rendered at 32:9: title and attract demo, main menu,
game options wheel, ship select, start-level select, level-1 help pop-ups,
memory-card load screen and high scores. Their final wide layout is the GL
presentation layer's job, already checked by the owner in V1 (each Aspect
looked right); the software renderer cannot show it.

Two-player: the main menu offers "two player game" (turn-based, single
full screen; it reached level 1 normally at 32:9) and "split two player game".
The menu's step size is not reliable under the automated controller, so the
split-screen mode was not reached; it remains unverified, as does any wide
layout for it.

### Limits

- This host has OpenGL 1.1, so the presentation layer (stretch-back, HUD and
  menu placement) was not exercised; the guest-side projection was. A software
  OpenGL 3.3 driver was not installed here (running a downloaded extractor needs
  the owner's permission).
- One controller route through level 1; other levels, enemies and the boss are
  not covered. Two-player split screen not reached.
- Performance at 32:9 on the owner's PC was measured in V1: 59.94 fps gameplay,
  emulation thread about 10.4 ms of 16.7 ms.

## V2 frame pacing and F1 bar (dev, 0.2.2, unreleased) — 2026-11-24

VERSION stays 0.2.2; the compiled-game inputs change (one rebuild,
`2b8b9bfe…`). Native selected code, coverage (38.4156% / 61.5844%), guest
clocks and the framework checkout are unchanged.

### Why motion judders at 144 Hz

The game produces 59.94 frames per second. On a 144 Hz panel that is 2.40
refreshes per frame, so frames are shown for 2 or 3 refreshes in turn. In
windowed and borderless modes the pinned framework deliberately turns driver
VSync off on NVIDIA with the render thread (Windows composes the window) and
paces with its wall clock; in exclusive Fullscreen it keeps a swap interval of
1 and the same wall-clock cap. Neither can divide 144 evenly. Even pacing
needs a refresh that is a whole multiple of 59.94 (60 or 120 Hz), or a
variable-refresh display (G-SYNC Compatible / FreeSync), which the owner
prefers not to enable.

### Changes

- Fullscreen refresh (`fullscreen_refresh`, 0 Desktop default, 1 Smoothest for
  60 fps), in the game's Video menu (second row, desktop only) and F1. Smooth
  picks, at the desktop resolution, the listed refresh closest to 59.94 Hz
  within 0.5% (59.970 Hz on the owner's 49-inch); only when there is none, the
  highest rate whose division by a whole number is within 0.5% of 59.94
  (such as 119.97 or 240 Hz). It is passed to the
  framework's existing exclusive-mode helper through its own
  `PSX_EXCLUSIVE_REFRESH_HZ` input, which only accepts a listed rate at the
  desktop resolution and otherwise keeps the desktop's; a developer value of
  that variable still wins. A change while already in Fullscreen re-selects
  the mode in place; SDL restores the desktop mode on leaving Fullscreen.
  Windowed and Borderless are unaffected. The framework re-probes the refresh
  each second and re-applies its own cadence (see the timing check below).
- Timing correction (owner review, 2026-11-24). The first V2 build picked
  119.97 Hz. Rates on the owner's 49-inch at 3840x1080, from the owner's V1
  report (SDL, from Windows' DXGI rates, three decimals): 144.000, 119.970,
  99.960, 59.970 Hz; Windows' own mode list there is 144, 120, 100, 60 and 59,
  and both of its 60 Hz-class entries resolve to the single 59.970 Hz mode, so
  there is no exact 59.94 Hz mode. The framework paces the game at exactly
  1000/59.94 ms (16.68335 ms). At 119.970 Hz two refreshes take 16.67084 ms, so
  each frame is 0.01251 ms late and one frame is held a third refresh every
  666 frames, about every 11.1 s (not 22 s as first written). At 59.970 Hz the
  framework's own match (within 2%) re-paces the game to the panel's reported
  rate (16.67500 ms) with driver VSync owning the cadence in exclusive
  Fullscreen, so each frame gets one refresh; the game runs 0.05% faster.
  A 10-minute simulation of frames ready at the pacer's deadlines and shown
  from the next refresh gives: 144 Hz, 40% of frames held 3 refreshes
  (constant judder); 119.970 Hz, one long frame every 11.1 s; 59.970 Hz with
  agreeing clocks, none; with the PC and graphics-card clocks 50 ppm apart,
  one every ~300 s (a faster game is held back by the swap queue instead).
  Smooth therefore now prefers the 60 Hz-class mode. The trade-off is a
  60 Hz panel instead of 120 Hz: slightly later display of each frame.
- F1 Settings reports refreshes per game frame and whether that is even, with
  advice when it is not, and which rate Smoothest uses.
- F1 menu bar: F1 now reveals the bar when it is hidden for any reason,
  including autohide (the pointer is hidden in fullscreen, so hovering the top
  edge was impractical), and hides it again on the next press. With autohide
  the preference is kept; focus loss returns to autohide.
- The game Video menu has seven rows (spacing 18 from y 60) to fit the new row
  above the existing footer.

### Local validation (this host)

| Check | Result |
| --- | --- |
| Smoothest picks 59.97 from the 49-inch's 144/119.97/99.96/59.97, 119.97 when 59.97 is absent, 59.94 over 60, 240 over 120, 60 from 60/75, none from 144-only, 100/72/50 or 61/59; refreshes per frame 2.40 uneven at 144, 2.00 even at 119.97, 1.00 at 59.97 | pass (tests/test_display_options.py) |
| Option default, range and persistence; the choice is set before the video mode is applied; developer override respected | pass |
| F1 rule: without autohide F1 toggles as before; with autohide it reveals and re-hides without changing the preference; it restores a switched-off bar; an open menu always shows it | pass |
| Video menu: seven rows at y 60-168, hint at 206, refresh row toggles both ways, all earlier rows, values and wraps | pass (tests/test_game_options.py) |
| Real normal build with V2 (two jobs) | pass (compiled-game inputs `2b8b9bfe…`) |
| Real game, software renderer: Video menu reached through the game's own options wheel shows all seven rows inside the footer | pass |
| Exclusive refresh switching, smoothness, F1 report on a real 144 Hz panel | pending, owner's PC (this host has a 32 Hz remote display); the owner's copy builds and starts at `35885323…` and the checks are deferred by the owner |

Found while testing, not changed: with the software renderer the options
file is not loaded, because loading follows the window update's OpenGL-only
early return; OpenGL players are unaffected.

## V1.2 menu parity and game extras in F1 (feature branch, 0.2.2, unreleased) — 2026-11-24

Requested by the owner after V1.1: the game's own Video menu and F1 Settings
offer the same display settings, and the game's Extras menu can also be set
from F1. This supersedes the V1 note that Aspect and VSync are "not in the
game's own Video menu". VERSION stays 0.2.2; the compiled-game inputs change
again (one rebuild, `17f52b05…`). Native selected code, coverage
(38.4156% / 61.5844%), guest clocks and the framework checkout are unchanged.

- Part A, game Video menu: six rows in this order: video mode (desktop only),
  HUD scale, HUD position, aspect, vsync, render resolution (slider). Same
  options and value steps as F1; labels use letters and digits only, because
  the game font's punctuation is not assumed.
- Part B, F1 Settings: adds Window mode (desktop only), HUD scale and Render
  resolution (25-200% in 5% steps) next to the V1/V1.1 settings. Both menus
  read and write the same nitrogen-options.ini values.
- Part C, F1 Game extras: lens flare (diamond cut, damp, sharp), lens type
  (normal, narrow, wide), camera roll (follow player, locked), hatching order
  (preset, random), bonus weapons (fixed, selectable).

### Part C investigation (this host, real game, private output)

The guest RAM was snapshotted while the game's Extras menu changed each
option. Each option is one byte, default 0, which changes only when that
option changes and matches its label: 0x8008FF7C lens flare, 0x8011B4BF lens
type, 0x8011B4BE camera roll, 0x8011B50D hatching order, 0x8011B470 bonus
weapons. With all five set to non-default values, the bytes kept their values
through the menus, level 1, bonus tally, level complete, next-level select
and level-2 gameplay (every 1500 frames). The four routines already guarded by
the Video, Audio and title bindings kept their verified hashes at every sample.
Not sampled: high-score entry, game over, tutorial, two-player and attract
demo; whether a mid-level change applies immediately is decided by the game.

### Part C design

- The game owns the values. F1 never stores them in nitrogen-options.ini; only
  the game's own memory-card set-up save keeps them, as with its Extras menu.
- F1 only queues a request (one atomic per option, validated against the
  option's range). The game thread applies it in the per-frame window update,
  between guest frames, with any renderer; never headless or in linked play.
- A request is written only while the verified Video and Audio binding code is
  resident (the same FNV identity the game menus use) and every one of the
  five bytes holds a valid value. Otherwise the section reads "Not available
  right now", is greyed out, and a request is dropped rather than replayed.
- F1 shows the published game values, with a pending request shown at once.

### Local validation (this host)

| Check | Result |
| --- | --- |
| Video menu: six rows at their positions, navigation and wraps, HUD position, aspect (with wrap), vsync and render steps through the real menu callback | pass (tests/test_game_options.py) |
| F1 and Video menu offer the same six options and set the shared keys | pass (tests/test_display_options.py) |
| Extras service through the real code: queued until the next frame, written once, game-side changes shown, out-of-range requests ignored, any invalid byte makes it unavailable with nothing written, requests made while unavailable dropped, non-resident Video or Audio code makes it unavailable | pass (tests/test_game_options.py) |
| Request queue: choices, validation, latest request wins and is shown, no service means dropped, unavailable never shows stale values; runtime wiring before the OpenGL-only early return, not headless or linked | pass (tests/test_display_options.py) |
| Real normal build with V1.2 (two jobs) | pass (compiled-game inputs `17f52b05…`) |
| Real game, software renderer, F1-equivalent requests from the measurement fixture: main-menu requests (lens type wide, bonus selectable) written within 100 frames; an invalid value ignored; the game's own Extras menu then showed wide and selectable; mid-level requests (camera roll locked, lens flare damp) written at the next frame; available and unchanged through level 1, bonus, level complete, next-level select, level 2 and high-score entry | pass |
| First real-game run found the service skipped under the software renderer (it sat after the OpenGL-only early return); moved before it and rerun | fixed |
| Owner's PC (inputs `17f52b05…`, installed from `236f5a7`): Video menu and F1 share the display settings; Game extras match the game's Extras menu; Lens type and Camera roll changed from F1 during level 1 applied straight away | pass — V1.2 validated on the owner's PC |

The measurement fixture's `extra_rel` and `ram_dump_rel` keys are maintenance
aids for this check; their output stays in the private `.local` folder.

## V1.1 HUD position (feature branch, 0.2.2, unreleased) — 2026-11-24

Requested by the owner after the V1 hardware checks: a setting, next to Aspect,
that keeps the HUD and menu footers centred on wide game areas, switchable at
any time. `hud_layout` in nitrogen-options.ini: 0 Edges (default, the original
placement), 1 Centred (16:9). Centred anchors edge-anchored UI roles (corner
HUD, score stack, weapon panel and footers) to the edges of a centred 16:9
region when the game area is wider than 16:9; centre-anchored phrases, menus
and the 3D world are unchanged, and a game area of 16:9 or narrower is
identical in both modes. It is in F1 Settings (live) and the launcher's
Settings file. VERSION stays 0.2.2; the compiled-game inputs change again (one
rebuild); native selection, coverage (38.4156% / 61.5844%), clocks and the
framework checkout are unchanged.

| Check | Result |
| --- | --- |
| Edges reproduces the original anchors exactly; Centred places 3840x1080 HUD edges at 1000 and 2840 (a 1920-wide region); centred parts never move; 16:9 and narrower game areas are identical in both modes; invalid values ignored | pass (tests/test_display_options.py) |
| Persistence: default Edges, saved Centred, invalid value ignored; launcher read/save and range | pass |
| Measurement preset `v1` adds a Centred run (`v1-hud-centred`) and records the HUD layout | pass (tests) |
| Real normal build with V1.1 (two jobs) | pass (compiled-game inputs `694e84a8…`) |
| Owner's PC (`v1` preset, inputs `694e84a8…`): Edges golden captures 26/26 identical to V0; Centred captures differ from Edges; live switching in menus and gameplay confirmed by eye | pass — V1.1 validated on the owner's PC |

## V1 owner hardware validation — 2026-11-24

On the owner's main PC (GTX 1080 Ti, driver 582.78, Samsung C49HG9x 3840x1080
at 144 Hz), V1 candidate `aa483b0` installed into a test copy (inputs
`eb7c7f58…`, normal build; the owner's normal folder kept its accepted build).
All results below passed; V1 is validated on the target hardware.

| Check | Result |
| --- | --- |
| Auto against V0 | 26/26 golden captures byte-identical to the owner's V0 quick report (inputs `4dc2fa4b…`); the Auto run at the full 3840x1080 desktop produced exactly V0's borderless captures |
| Aspect game areas | 32:9 3840x1080 (identical to Auto), 21:9 2520x1080, 16:9 1920x1080, 4:3 1440x1080; world rasters 10803, 7090, 5402, 4051 x 2304 |
| Bar positions | all 15 presented captures: picture exactly at the expected columns (0-3839, 660-3179, 960-2879, 1200-2639), centred at 1919.5, everything outside pure black |
| 3D limit | 7680x2160 at 200% requested 43211x9216 and used 32768x6988 ("texture limit"), not 100%; gameplay 59.7 fps with about 30 frames of 25-40 ms at that oversized extreme |
| 200% in F1, F1 report | confirmed by the owner |
| VSync | Off setting in effect (measured); live switching off and on while playing confirmed by the owner |
| Visual look of each Aspect | confirmed by the owner |

Observed separately, not caused by V1: with the F1 menu bar on autohide, the
bar could not be revealed in fullscreen; the owner switched to windowed,
turned autohide off and returned. Recorded as an existing menu-bar issue.

## V1 Aspect, VSync and 3D-size reporting (feature branch, 0.2.2, unreleased) — 2026-11-24

V1 adds two player settings, in the launcher's Settings > Display and in F1
Settings only (the game's own Video menu is unchanged), and reports what the
renderer actually produced. VERSION stays 0.2.2; no tag or release. Native
selected code, its coverage (38.4156% linked / 61.5844% outside), guest clocks
and the pinned framework checkout are unchanged. The compiled-game inputs
change, so players get one rebuild; saves, settings, mods and imports are kept.

### Behaviour

- Aspect (`aspect` in nitrogen-options.ini): 0 Auto, 1 Original 4:3, 2 16:9,
  3 21:9, 4 32:9. Auto takes the original path unchanged: the whole game area,
  its gcd-reduced ratio and the same projection. A fixed ratio uses the
  largest centred rectangle of exactly that ratio (21:9 is 21/9 = 2.333, not
  64:27). The game area, interface scale, world raster and internal scale are
  computed from that rectangle; the display aspect given to the renderer is the
  ratio itself, so the existing presentation letterbox draws the same
  rectangle with black bars. `nitrogen_window_game_area` uses the letterbox's
  integer arithmetic; a test checks they agree at every size tested.
- VSync (`vsync`): 1 On keeps the framework's own startup choice (which a mod
  or configuration may already have forced off); 0 Off presents immediately.
  A change goes through the framework's `apply_present_cadence`, live. A
  developer `PSX_VSYNC` value still wins and is shown as such.
- Defaults are Auto and On. Files written before V1 have neither key and read
  as Auto and On; invalid values are ignored.
- 3D scene clamping: the GL owner publishes GL_MAX_TEXTURE_SIZE, and any world
  target the driver refuses. A request above the limit uses the largest
  proportional size within it (for example 7680x2160 at 200% requests
  43211x9216; with a 32768 limit it uses 32768x6988, not 100%). After a refusal,
  sizes step to below 90% of the refused size. Before the first limit report
  (one frame) the previous fallback can still occur.
- F1 Settings shows the window, game area, interface scale, requested and
  actual 3D size with the reason, the GPU texture limit, the picture surface
  and its framework limit, the VSync in effect and the display refresh, with a
  note when that refresh cannot match the game's 59.94 Hz. The framework's
  surface report waits for the render thread, so it is read at most once a
  second and only while Settings is open.

### Local validation (this host)

| Check | Result |
| --- | --- |
| Aspect fit equals the presentation letterbox (all four ratios, 5,168 window sizes), exact 21:9, Auto = window | pass (tests/test_display_options.py) |
| 3D clamping: fits below the limit unchanged; 7680x2160 200% uses the largest proportional size; refused allocations step down and keep the smallest refusal; movies/2D report "not used" | pass |
| VSync policy (On keeps framework choice, Off, forced-off kept, PSX_VSYNC wins) and live wiring to apply_present_cadence | pass |
| Option persistence: defaults, pre-V1 files, invalid values, launcher read/save | pass |
| Launcher Settings: Aspect/VSync start at Auto/On and save through the bridge; layout at three window sizes | pass (12 browser tests) |
| Real normal build compiles and links with V1 (two jobs) | pass |
| Software-renderer golden route on the rebuilt game against the pre-V1 local V0 golden report | pass: 13/13 captures identical to the pre-V1 report (build 618eea77), and 13/13 between the two V1 runs |
| OpenGL presentation: bars, Auto pixel identity, F1 report, VSync switching, clamping on a real GPU | not possible here (OpenGL 1.1 host); passed on the owner's PC (below) |

The software renderer does not run the window-display path, so the local
golden check covers the game and the new option keys, not the V1
presentation. The `v1` measurement preset and `--baseline` comparison are for
the owner's OpenGL check: Auto golden screenshots must match V0 byte for byte.

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
  multiple. Measured on hardware: see the owner baseline below.
- Present times are swap-call returns, not scanout; compositor queueing is not
  visible.
- Fixture-selected exclusive modes use SDL directly; the player's Fullscreen
  setting uses the framework helper at the desktop mode and is measured as its
  own run.

### Level-entry stutter

Still OPEN. The long route crosses two level entries: level 1 (0-330 frames
after the start press) and level 2 (600 frames from the Cross that starts it),
labelled as their own scenes, with every hitch attributed. The `stutter` preset
runs the long route three times. The owner's hardware results below reproduce
the level-2 entry hitches and locate them on the emulation thread; the code
responsible is not yet identified.

### Owner hardware baseline — 2026-11-24

Measured by the owner with the unchanged normal 0.2.2 build (inputs
`4dc2fa4b…`, player executable SHA-256 unchanged before and after): Windows 10
19045, AMD Family 23 Model 8 (16 logical CPUs), NVIDIA GeForce GTX 1080 Ti
(11264 MiB, driver 582.78, OpenGL 3.3, GL_MAX_TEXTURE_SIZE 32768), Samsung
C49HG9x at its native 3840x1080 143.999 Hz plus a C27F390 at 1920x1080 60 Hz.
Variable refresh is believed to be off and is not required. Every mode above
3840x1080 that the 49-inch lists (4206x1183 to 7680x2160) is above native,
i.e. GPU-scaled (DSR), not a panel mode. Presets run: probe, `quick` (11 runs)
and `stutter` (6 runs); all runs completed. Reports and captures stay in the
owner's `.local/measurements`.

| Result | Value |
| --- | --- |
| Golden determinism | 26/26 identical guest captures in each preset; the three long routes also produced identical captures at all 29 points through high-score entry |
| Gameplay rate, every size/mode tested | 59.94 fps (one display flip per VBlank), including 5120x1440 windowed-oversized at 200% |
| Emulation thread, level-1/2 gameplay | about 10.4 ms p50, 11-12 ms p99 of the 16.68 ms frame |
| Render-thread CPU, gameplay | about 2.2-2.5 ms p50 |
| Exclusive 3840x1080@144, Borderless, Fullscreen | completed; same cadence as windowed |
| VSync on vs off (exclusive 144 Hz) | indistinguishable at the swap call |
| Presentation (swap-call returns) | gameplay swaps about every 16.6 ms; swap calls return in about 0.04 ms (non-blocking); movies present at the 144 Hz refresh (the ~4,200 one-refresh intervals per run) |

Memory, driver-reported whole-GPU increase over the idle baseline compared with
the calculation above:

| Window | 3D | Calculated MiB | Measured increase MiB |
| --- | ---: | ---: | ---: |
| 1920x1080 | 100% | 255 | about 260 |
| 3840x1080 | 100% | 690 | about 790-800 |
| 3840x1080 | 200% | 1260 | about 1370 |
| 5120x1440 | 100% | 1254 | about 1405 |
| 5120x1440 | 200% | 2266 | about 2420 |

The 5120x1440 VRAM surface is exactly 943,718,400 B (S = 15), confirming the
0.94 GB estimate. 200% at 5120x1440 (28808 px wide) fits the 32768 limit; 200%
at 7680x2160 (43211 px) would not and was not run in `quick`.

Hitches over 25 ms, after separating measurement effects:

- Level-1 and level-2 gameplay, pause, bonus tally and high-score entry: none
  from the game in any run. The 65-70 ms frames at 5120x1440 and one 26 ms
  frame at 3840x1080 200% fall exactly two frames after a present capture
  (glReadPixels), so they are measurement cost.
- Level-2 entry, reproduced in all three long runs at the same frames (11610,
  11622, 11625, 11641 and 11642 after the level-1 start): 26, 57, 47, 31 and
  25 ms, with the emulation thread busy 20-50 ms in those frames. No capture
  is near them. This is the game thread running far over budget, not GPU,
  display or presentation timing.
- Level complete (frames 10131-10135): five consecutive 34-42 ms frames, the
  emulation thread busy 34-37 ms, in all three runs; smaller 26-30 ms frames
  follow, and two 27 ms frames in next-level select.
- Level-1 entry (frames 5-11): 25-29 ms, emulation-bound, but only in the
  1920x1080 windowed golden runs; not in 3840x1080 windowed or exclusive runs.
  Unexplained; small.
- Menus: frames 721 and 1681 (55-90 ms, emulation-bound) in every run.
- Frames 180-182 (about 29 s together) are the opening movies playing inside
  single guest frames, not hitches.

144 Hz interpretation: the predicted 2/3-refresh alternation is not visible at
the swap call, because the driver queues frames and the call returns at once.
The panel still scans out at 144 Hz, so each 59.94 Hz frame must be shown for
two or three refreshes; judder is expected unless variable refresh is used or
the refresh is an exact multiple (120 Hz, not run). The tooling cannot observe
scanout.

Tooling limits found on hardware, and how the analysis now handles them (the
figures above come from the earlier analysis; the fixture and its recorded data
are unchanged):

- `render_thread_gpu_ms` read about 16.7 ms p50 at every size, including
  1920x1080. The pinned renderer's GL_TIME_ELAPSED bracket contains the
  frame's swap (its CPU figure subtracts the swap; the query cannot), so the
  value includes swap-chain waiting. It is now reported as
  `render_thread_gpu_span_ms` with that explanation, and no hitch is
  attributed to the GPU; idle-thread hitches read "waiting (GPU or
  swap-chain back-pressure, I/O, scheduling or pacing; not separable)".
- The exclusion around captures did not cover the frames after a present
  capture. The analysis now excludes the capture's guest frame and the three
  after it from frame times, hitches and present spacing, and counts them.
- Opening movies were counted as hitches and mixed into present spacing.
  Guest frames with 30 or more host presents are now a `native-movie` scene,
  excluded from frame times and hitches, with movie presents reported
  separately; game present spacing is also given per scene. This detection is
  checked with synthetic data only and is first confirmed on the next
  OpenGL run.

### Closure

The owner reviewed these findings and closed V0 on 2026-11-24. `full` (120/60
Hz modes, 7680x2160) was not run and is not required.
