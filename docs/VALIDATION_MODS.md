# Mod framework validation

Results for the unreleased mod-framework work on `dev` (see [MODS.md](MODS.md)).
Release validation stays in [VALIDATION.md](VALIDATION.md).

## Phase 0 framework mod activation probe (dev, unreleased) — 2026-11-23

Source inspection of the pinned framework (f42d2c0) shows that mod catalog
scan, state.toml selection, plan commit and plugin activation run on every
Nitrogen launch, outside the recomp-ui launcher blocks. See [MODS.md](MODS.md).
The new NITROGEN_MODS option (default OFF) links only the original
nitrogen.mod-probe plugin, which reads and writes no guest state.

Local Windows run of tools/ci_runtime_build.py with the pinned LLVM-MinGW,
CMake and Ninja (local job count raised from 2): the real runtime booted
unattended against the original synthetic program and a zero-filled
synthetic disc file. Every scenario used a fresh staged copy of the runtime:

| Build | Package state | Result |
| --- | --- | --- |
| OFF | none | stays running; no activation |
| OFF | probe enabled | exit 1 before BIOS: `trusted plugin is unavailable: nitrogen.mod-probe` |
| ON | none | stays running; no activation |
| ON | probe enabled | `mod probe activated` before BIOS load, then first VBlank callback; receipt activations=1, vblanks=1 |
| ON | probe installed, disabled | stays running; no activation |
| ON | manifest syntax error | `mod manifest ignored`; stays running; no activation |
| ON | feature names unlinked plugin id | exit 1 before BIOS: `cannot launch with selected mods` |

The existing save-path, keyboard, status/title, media, native-slice (384
calls), native-boundary (122 cases) and input smokes then passed, rebuilt
with NITROGEN_MODS=OFF. All 174 Python tests pass without skips when the
pinned MinGit, CMake, Ninja and LLVM-MinGW are on PATH. With this host's MSYS
git, seven portable git fixtures crash in `git fetch` (0xC0000005); the same
seven also fail on the unmodified baseline. The source audit passes for 193
reviewed text files.

Limits: no N2O disc was available on this host, so the probe has NOT been run
against the player's game. The synthetic guest never reported game start
(game_started=0); that path is unverified. Linux and GitHub CI have not run
this change. The compiled-game input fingerprint changes to
72bf1cd167126460522d9a53d349bd2bff6205619a0dfc393b8a53e947d39057 because
CMakeLists.txt, cmake/ and native/ changed. A release containing it would
rebuild player games once, even with the option OFF. VERSION, TESTING.md,
native selection, coverage 38.4156% linked / 61.5844% outside and clocks are
unchanged.

### Real USA disc run (dev, unreleased) — 2026-11-23

The owner's USA dump is a per-track split CUE (11 BINs). `tools/n2o_disc.py`
accepts only the verified single-BIN layout and was not changed. Read-only
concatenation of the BINs in CUE order gives 540,680,112 bytes with SHA-256
99b2e8c6c61dfaf64755e5164f25b6f6ad4598556ecfef585e21dbc6128757ae, exactly the
`configs/disc_usa.json` identity. Each file boundary equals the expected pregap.
A private merged copy plus a single-BIN CUE derived from that verified layout
was written under `.local/disc`. The unmodified importer then verified the
disc and extracted SLUS_006.37, N2O.EXE and NEW.EXE with matching hashes into
`.local/installation`. The original files were only read.

`build_runtime.py --cmake-arg=-DNITROGEN_MODS=ON` generated and linked the
private runtime (configure reported `nitrogen.mod-probe`). Memory cards used
a fresh disposable `.local` folder. Launched through `run_runtime.py` with the
probe package installed and the receipt inside `.local`:

| Run | Result |
| --- | --- |
| Probe enabled | `mod probe activated` before BIOS load; first VBlank callback; game start observed at VBlank 80. Receipt: activations=1, vblanks=80, game_started=true. Opening movie, credits, loading and the title/attract screen ("press X to start") displayed. Closing the window exited 0. |
| Probe removed (baseline) | Same title/attract screen; no probe output or receipt; exit 0. `mod_probe.py remove` left no state.toml. |

Host limit: this Windows Server session exposes only OpenGL 1.1 (GDI Generic).
The default OpenGL renderer and `--renderer software` both failed in
`SDL_CreateRenderer` (exit 1, after activation, before any guest frame). The
runs above used `--renderer software` with SDL_RENDER_DRIVER=software; no code
changed. No controller input was sent, so menus beyond the title, gameplay,
saves, CD audio during play and the OpenGL renderer were not exercised here.

## Phase 1 mod manager (dev, unreleased) — 2026-11-23

Adds `tools/modkit` (durable `.psxmod` store under `.local/mods`, selections,
runtime staging), launcher service commands `mods`, `mod_install`,
`mod_enable` and `mod_remove`, a native `.psxmod` picker, and a launcher Mods
page. Full schema validation and plugin availability come from the built
runtime's own `--audit-mod-plugins` mode. No framework file, compiled-game
input, VERSION, native selection or clock changes. The compiled-game input
fingerprint is unchanged from the staged Phase 0 value. Coverage remains
38.4156% linked / 61.5844% outside.

| Check | Result |
| --- | --- |
| 23 new synthetic modkit tests (archives, manifests, install, duplicates, missing plugins, other game, enable/disable, removal, rebuild persistence, staging and restore, foreign packages, launcher service and play staging) | pass |
| Full Python suite, pinned MinGit/CMake/Ninja on PATH | 197 tests, no skips, pass |
| Launcher `npm test`, production build, Playwright (9 tests, 2 new Mods tests) | pass |
| Synthetic runtime CI, NITROGEN_MODS=OFF: real plugin report, framework parser refuses `resolver = "bogus"`, probe package shows missing plugin and cannot be enabled | pass |
| Synthetic runtime CI, NITROGEN_MODS=ON: install, framework validation, staging, probe activation with VBlank receipt, disable restores an unmodded launch | pass |
| All earlier runtime smokes in the same CI run | pass |
| Real USA build (ON): modkit install validated by the game's parser, staged, launched through `run_runtime.py`; receipt activations=1, vblanks=80, game_started=true; title/attract screen displayed; window close exited 0 | pass |
| Real USA build: disable and stage removed staged packages and receipt and restored the framework's earlier `state.toml` | pass |

Limits: Rust was not compiled on this host, so the new `choose_mod` Tauri
command, the packaged launcher and its native file picker are unverified until
platform CI builds them. The Mods page was exercised only through the browser
adapter. Linux was not run locally. The real-game run used the software
renderer as in Phase 0. Per-feature switches, options and resource paths are
not managed. One installed version per package id.

## Phase 1 packaged Windows launcher validation (dev, unreleased) — 2026-11-23

Supersedes the Rust/packaged-launcher limits above for Windows. No platform CI
has run: nothing has been pushed. The launcher was built locally with the
repository's `tools/build_launcher.py` (Rust 1.100.0 stable, MSVC x64 Build
Tools, Node 24, two Cargo jobs) and packaged with `tools/package_windows.py`
under official CPython 3.13.17. The Rust code, including `choose_mod`, compiled
with no errors or warnings. Package: 2,510 files, verified inventory, SHA-256
69cab8d33722d53a225d9d90c3631be7df5c522c3e218c35fa40b9e32f1d544b. No source
change was needed.

The ZIP was extracted to a path containing spaces under `.local`. Only the four
pinned tool archives were pre-seeded; framework, SDL, generation and compilation
ran as a normal first setup from the packaged launcher.

| Check (packaged launcher.exe, real USA disc) | Result |
| --- | --- |
| `--self-test` (WebView2 frontend, Home/What's New/Mods/Settings at two sizes) | pass |
| Mods empty state; Install package… opens the native `.psxmod` picker; cancel changes nothing | pass |
| Three synthetic packages installed through the picker; without a build all show Not checked yet | pass |
| Mods controls disabled during first setup | pass |
| First setup from the launcher built the game; unmodded game reached its intro and title | pass |
| Player build (NITROGEN_MODS=OFF): `resolver = "bogus"` shows Invalid package with the framework reason; probe shows Missing plugins with a disabled switch; `psx.bezel` package shows Compatible | pass |
| Enable bezel package; Play staged it (`mods/installed`, format-2 `state.toml`, receipt, backup); framework accepted the plan; game reached the title screen | pass |
| Disable; Play removed the staged package and receipt and restored a byte-identical `state.toml`; game ran normally | pass |
| Remove with confirmation deleted the package and metadata | pass |
| Probe activation (test-only ON build of the same install): Play detected changed build options, rebuilt, staged the probe; receipt activations=1, vblanks=80, game_started=true | pass |
| Durable `.local/mods` byte-identical across that launcher rebuild | pass |
| Real copied-updater update 0.2.2 → 0.2.3 (`tools/test_portable_update_windows.py`): updated launcher self-test passed; `.local/mods` byte-identical; probe still enabled and compatible; next Play reused the build | pass, see note |
| Disable probe; Play restored an unmodded launch (no activation, empty `installed/`, baseline `state.toml`) | pass |

Notes and limits:

- The ON build was produced by running the packaged app's own
  `Backend.prepare()` with `-DNITROGEN_MODS=ON` added. Player builds still
  have no switch for it. The later launcher rebuild kept the cached ON value.
- The desktop session disconnected after the remove check. Enabling the probe,
  its Play and the update steps used the same allowlisted service commands
  (`mod_enable`, `play`) through the packaged Python, not GUI clicks.
- The update fixture reported failure because its fixed 120-second restart
  deadline expired. On this host the runner finished the 2,510-file update and
  restarted the launcher about 3.5 minutes after it started. The transaction
  completed and the restarted self-test report passed. This is the existing
  fixture's timing, not a mod change.
- This host needs SDL_RENDER_DRIVER=software (environment only) for the game
  window, as in Phase 0.
- Existing behavior observed, not changed: the WebView2 profile is stored in
  `%LOCALAPPDATA%\org.nitrogen-squared.launcher`, outside the portable folder.
- Linux packaging and GitHub platform CI have not run.
