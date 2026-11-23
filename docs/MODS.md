# Mods: framework packages and the Nitrogen mod manager

Players install `.psxmod` packages from the launcher's **Mods** page. The
pinned framework loads them; Nitrogen keeps them in permanent storage and
hands the enabled ones to the game build before each launch. Sections from
[Package installation](#package-installation) onward describe that manager;
the earlier sections record the verified framework behavior it relies on.

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
`mods/installed` and `mods/state.toml` live beside the compiled game, and a
rebuild that replaces that directory discards them. The mod manager therefore
keeps the durable copy in `.local/mods` and recreates both before each launch
([runtime staging](#runtime-staging)).

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
[mod validation](VALIDATION_MODS.md).

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

## Package installation

In the launcher, open **Mods** and choose **Install package…**, then select a
`.psxmod` file. The package is checked, copied into permanent storage and left
**disabled**. Turn its switch on to use it from the next launch; turn it off or
choose **Remove** to stop using it. Mod controls are locked while the game is
being prepared or is running. Changes never alter the original disc.

Each card shows the package name, version, description, author, licence,
package id, required plugins and a compatibility status:

| Status | Meaning |
| --- | --- |
| Compatible | The current game build parsed the manifest and contains every required plugin. |
| Not checked yet | No game build exists yet. It can be enabled; launch re-checks it after preparation. |
| Missing plugins | The build lacks a named trusted plugin. It cannot be enabled. |
| Invalid package | The game's own manifest reader refused it; the reason is shown. |
| Different game | Its `[[target]]` entries name neither `SLUS-00637` nor `*`. |

Developers can do the same from a source checkout:

```sh
python tools/modkit install path/to/package.psxmod
python tools/modkit list                 # JSON: details, enabled flag, compatibility
python tools/modkit enable <package-id>  # or: disable / remove
python tools/modkit plugins              # plugin ids linked into the current build
python tools/modkit stage                # needed before a direct run_runtime.py launch
```

`--store` (default `.local/mods`) and `--work-dir` (default `.local/runtime-dev`)
select other locations. The portable launcher stages automatically.

### What is checked at install

Archive rules match the framework installer (`parse_zip`, `safe_archive_name`):
a ZIP with `manifest.toml` at its root, at most 4096 entries and 256 MiB
expanded, stored or DEFLATE entries, no encryption, and no absolute,
drive-qualified, `.` or `..` paths. Duplicate paths that differ only by case are
also refused, because Windows cannot store both. The manifest must be UTF-8
TOML with `format_version` 1–11, a valid id, a semantic version and a name.

The rest of the schema is not reimplemented. When a game build exists the
manager runs that executable with the framework's `--audit-mod-plugins`
option. It parses each manifest with `ModPackageManager::read_manifest` and
lists every registered plugin id, without starting emulation. A package the
game would ignore is refused at install, or shown as invalid when it was
installed before the first build.

One version of each package id can be installed at a time. Installing another
version of an installed id is refused until the old one is removed.

## Package storage

Everything the manager owns is under `.local/mods`, outside the build output:

```text
.local/mods/
  packages/<id>/<version>/      extracted package contents, never edited after install
  metadata/<id>/<version>.json  archive name, size and SHA-256, install time, manifest summary
  selections.json               which installed package ids are enabled
  runtime-plugins.json          plugin ids reported by the last checked game build
  staging/                      scratch while replacing a build's mods tree (emptied after use)
  tmp/                          scratch while extracting an archive (emptied after use)
```

Rebuilding the game replaces `.local/runtime-dev`, not `.local/mods`, so
installed packages and choices survive rebuilds. Launcher updates preserve the
whole `.local` folder. **Open mods folder** opens `.local/mods`. Packages are
never copied into Git, source packages or releases.

## Plugin compatibility

A package's `[[plugin]]` entries name trusted C/C++ implementations that must
already be linked into the game. A `.psxmod` cannot add native code. The
plugin list is read from the actual build and cached by executable SHA-256, so
a rebuilt game is checked again automatically.

- Every Nitrogen build contains the framework's own plugins (for example
  `psx.bezel`, `psx.pgxp`, `psx.crt-filter`).
- `nitrogen.mod-probe` and future Nitrogen plugins exist only in builds
  configured with `-DNITROGEN_MODS=ON`. Player builds currently use OFF, so a
  package requiring them shows **Missing plugins** and cannot be enabled.
- Declarative packages (patches, overlays and options without plugins) are not
  limited by the build option.

## Runtime staging

Before each launcher **Play**, after any rebuild and before the game process
starts, the manager:

1. Re-checks every enabled package against the build. An incompatible or
   invalid package stops the launch with a message naming it.
2. Copies the enabled packages into a fresh `staging/` tree and moves it into
   place as `<exe>/mods/installed`. Disabled packages are not staged.
3. Writes `<exe>/mods/state.toml` in framework format 2: a `[[package]]` with
   the id and version, and an enabled `[[feature]]` for each of its features.
4. Records what it placed in `<exe>/mods/nitrogen-mods.json`.

`<exe>/mods/bundled` (framework build output) is never touched. An existing
`state.toml` that the manager did not write is kept as
`state.toml.before-nitrogen-mods-<time>`. Packages in `mods/installed` that the
manager did not place are never deleted; staging stops and names them instead.
When the last package is disabled, the next launch removes the staged packages
and the generated `state.toml`, and restores the earlier file.

If nothing is enabled and nothing was staged before, staging changes nothing,
so launches without mods are unchanged. The framework rewrites `state.toml`
during its plan commit; the manager regenerates it from `selections.json` on
the next launch. Package switches enable all of a package's features.
Per-feature switches, option values and resource paths are not managed yet.

## Developing a new mod

1. Read the framework guide `.local/psxrecomp/psxmod.md` (present after
   preparation) and its `docs/MOD_PACKAGES.md`. Use `format_version = 10`
   unless you need a format-11 field. Target `game_id = "SLUS-00637"`, or `"*"`
   for generic presentation features.
2. Use your own lowercase package namespace and increase `version` for every
   distributed change. Start features with `default_enabled = false`.
3. Include only your own work. Original game files, extracted assets, disc
   bytes, saves and `state.toml` must never be placed in a package.
4. Pack the folder's contents (not the folder itself):
   `python .local/psxrecomp/tools/psxmod_pack.py my-mod dist/my-mod-1.0.0.psxmod`.
   The packer only checks that a manifest exists.
5. Install it with the launcher or `python tools/modkit install`. The current
   build's parser reports schema errors at that point.
6. If the mod needs new host logic, write a trusted plugin. Add an original
   `native/mods/<name>.cpp` that registers a unique id (see
   `native/mods/mod_probe.cpp`), list it in `cmake/nitrogen_mods.cmake`, and
   build with `-DNITROGEN_MODS=ON`. Confirm the id with `python tools/modkit plugins`.
   Linked code is subject to the framework's PolyForm Noncommercial terms.
7. To test a changed package with the same version, remove and reinstall it,
   or increase the version.
