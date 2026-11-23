# Framework mod packages — Phase 0 findings

This records how the pinned PSXRecomp framework (`f42d2c004b3dd6ef6ccb83101b40ede55f24bf59`)
loads mods inside a Nitrogen runtime. It was verified against that checkout's
`runtime/src/main.cpp`, `mod_runtime.cpp`, `mod_packages.cpp`, `runtime.cmake`
and the headers `mod_plugins.h`, `mod_runtime.h`, `mod_packages.h` and
`func_override.h`. No framework file is changed. No mod is enabled by default.

## What the framework already does in every Nitrogen build

The calls below run outside every `RECOMP_LAUNCHER` block. `PSX_RECOMP_UI OFF`
therefore removes only the recomp-ui Mods page, not mod loading:

1. `mod_runtime_initialize(<exe-dir>/mods, game id, ...)` scans
   `mods/bundled/<id>/<version>/manifest.toml`, then `mods/installed/...`, and
   loads `mods/state.toml`. A manifest that fails to parse is reported as
   `psxrecomp: mod manifest ignored: ...` and skipped.
2. `mod_runtime_commit(disc)` hashes the selected disc image, resolves enabled
   features for `[game] id` (`SLUS-00637` for Nitrogen), and rewrites
   `mods/state.toml`. An unresolvable plan, for example an enabled feature whose
   trusted plugin is not linked, prints `cannot launch with selected mods` and
   the runtime exits with status 1 before guest boot.
3. `mod_runtime_activate_plugins()` runs activation callbacks of the committed
   plan before renderer/window creation. VBlank callbacks run from emulated GPU
   VBlank only for plugins in that plan.

`PRELOADED_MODS_DIR NONE` means Nitrogen ships no title-owned packages. The
framework still stages its own builtin packages into `<exe-dir>/mods/bundled`;
at this pin all of them have `default_enabled = false`.

Because `<exe-dir>` is the private build directory (`.local/runtime-dev/...`),
`mods/installed` and `mods/state.toml` currently live beside the compiled game.
A rebuild that replaces that directory can discard them. A launcher integration
must own a durable copy elsewhere in `.local` and place it before launch.

## Trusted plugins

A plugin is C/C++ linked into the executable. A static constructor registers it
under a stable id with `psx_mod_register_activation_plugin`,
`psx_mod_register_vblank_plugin`, `psx_mod_register_function_override` and
similar calls. Registration alone has no effect. A package `[[plugin]]` entry in
an enabled feature selects the id, and only then do its callbacks run. Archives
cannot supply native code.

## Build option and probe

`-DNITROGEN_MODS=ON` (default OFF) links Nitrogen's original plugins from
[`cmake/nitrogen_mods.cmake`](../cmake/nitrogen_mods.cmake). Currently this is
only `native/mods/mod_probe.cpp`. Its plugin `nitrogen.mod-probe` reads and
writes no guest state. When activated it prints `nitrogen: mod probe activated`
and the first VBlank callback. If `NITROGEN_MOD_PROBE_RECEIPT` names a file, it
also writes a JSON receipt there.

`tools/mod_probe.py` writes the original probe package and selection. It backs
up an existing `state.toml` and restores it on removal:

```sh
python tools/build_runtime.py --cmake-arg=-DNITROGEN_MODS=ON
python tools/mod_probe.py install --exe-dir <directory containing NitrogenSquared-dev>
NITROGEN_MOD_PROBE_RECEIPT=$PWD/.local/mod-probe-receipt.json python tools/run_runtime.py
python tools/mod_probe.py verify --receipt .local/mod-probe-receipt.json
python tools/mod_probe.py remove --exe-dir <same directory>
```

`--variant disabled|missing-plugin|bad-manifest` installs the negative cases.
`tools/ci_runtime_build.py` boots the real runtime unattended against the
original synthetic program. It runs these scenarios in both build modes; see
[validation](VALIDATION.md).

Changing `CMakeLists.txt` and adding `cmake/` and `native/` files changes the
compiled-game input identity, so existing player builds rebuild once even
with the option OFF.

## Notes for later phases

- `main_exe` patches apply to the boot executable `SLUS_006.37` at its entry.
  `N2O.EXE` and `NEW.EXE` are loaded later and overlap, so changes to them
  need a guarded trusted plugin, not a declarative `main_exe` patch.
- Each commit hashes the full selected image (the merged USA BIN is
  540,680,112 bytes) unless the framework's worker-safe preparation cache is
  enabled. This already happens in current builds. Its startup cost on player
  machines has not been measured.
- CD-DA replacement (`psx_mod_append_cdda_tracks`) is available to an activated
  plugin. It is not used yet.
