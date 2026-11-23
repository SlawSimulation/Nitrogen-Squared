# Soundtrack editor

The launcher's **Soundtrack** page replaces, reorders and previews N2O's ten CD
audio tracks and exports the result as a `.psxmod` package. Install the package
from **Mods** and enable it to hear it in game. Your disc, the asset extractor's
WAVE files and other installed packages are never changed.

## How N2O's CD audio works

N2O's disc has one data track and ten CD-DA audio tracks, disc tracks 2–11.
The verified USA image has these lengths (INDEX 01 to the next track's INDEX 00):

| Track | Sectors | Length |
| --- | ---: | ---: |
| 02 | 19,944 | 4:25.92 |
| 03 | 19,946 | 4:25.95 |
| 04 | 17,245 | 3:49.93 |
| 05 | 24,462 | 5:26.16 |
| 06 | 18,767 | 4:10.23 |
| 07 | 19,762 | 4:23.49 |
| 08 | 19,184 | 4:15.79 |
| 09 | 21,624 | 4:48.32 |
| 10 | 19,524 | 4:20.32 |
| 11 | 34,124 | 7:34.99 |

All ten are 44.1 kHz, 16-bit, stereo PCM in whole 2,352-byte sectors (588
stereo frames each), 481 MiB in total. The asset extractor's `music/track-NN.wav`
files contain those sectors byte for byte. Track names are drawn by the game
itself; Nitrogen has no verified list of them, so the editor shows numbers.

The game asks the emulated CD controller for numbered audio tracks. The pinned
framework lets an activated trusted plugin replace the controller's whole audio
table of contents with `psx_mod_append_cdda_tracks()`. Each entry is either an
audio track on the mounted disc or a verified resource of raw 2,352-byte CD-DA
sectors. Playback, volume, reports and save states stay with the native CD
controller. Data and XA (movie) sectors are unaffected.

## What the editor does

- **Browse.** Each slot 02–11 shows its source, length, format and state:
  Original, Moved (another original track) or Replacement.
- **Preview.** ▶ plays a slot's first 30 seconds; Preview playlist plays 8
  seconds of each slot in order. Previews play through the computer's audio from
  the launcher; the game is not started. Originals are read from your verified
  disc image.
- **Replace.** Choose a WAVE file for a slot. It must already be uncompressed
  PCM at 44.1 kHz, 16-bit, stereo (standard PCM or WAVE_FORMAT_EXTENSIBLE).
  Other rates, depths, channel counts, float or compressed files are refused
  with the reason; nothing is resampled or converted. Conversion copies the
  sample bytes unchanged into raw CD-DA sectors and pads the last sector with
  silence. Files shorter than one second are refused.
- **Reorder.** The arrows swap a slot's source with its neighbour, so any
  original or replacement can play in any slot.
- **Restore.** Return one slot, or all slots, to the original disc track.
- **Export.** Validate and write `.local/soundtrack/exports/<id>-<version>.psxmod`,
  optionally installing it in Mods at the same time.

There are always exactly ten slots. The game's buffers are sized for its ten
tracks, so the editor never adds or removes tracks.

### Size limit

The framework installs packages of at most 256 MiB expanded. Replacement
audio is limited to 255 MiB per package, about 25 minutes of CD audio (176,400
bytes per second). Replacing every track at its original length would need
481 MiB, so a single package cannot do that. Shorter replacements fit, and
unchanged slots cost nothing because they play from your disc. The same audio
used in several slots is stored once.

## Storage

```text
.local/soundtrack/
  project.json                 slot assignments and package details
  replacements/<sha256>.pcm    converted raw CD-DA sectors; unused files are removed
  previews/                    temporary preview WAVE files
  exports/<id>-<version>.psxmod
```

## Package format

```text
<id>-<version>.psxmod
  manifest.toml
  README.txt
  resources/playlist.txt
  resources/audio-1.pcm        only replacement audio, one file per distinct file
```

The manifest (format 10) targets `SLUS-00637` and declares one feature,
`soundtrack`, off by default, which selects the trusted plugin
`nitrogen.soundtrack`. Each shipped file is a required `[[resource]]` of
`format = "file"` with its exact `size` and `sha256`, so the framework verifies
it before the plugin can read it. The playlist is:

```text
nitrogen-soundtrack 1
2 disc 2
3 resource audio-1
...
11 disc 11
```

one line per slot, 2 through 11 in order: `disc <2-11>` plays that original
track from the player's disc, `resource <id>` plays a shipped file.

The framework reads verified bytes only from a selected path and does not allow
a package default for them. When the launcher stages an enabled package, it
selects files that a package ships as `resources/<resource-id>.<ext>` and that
match the declared size and SHA-256, writing their staged paths under
`[feature.resources]` in `state.toml`. A package needing other resource files
shows **Needs files** in Mods and cannot be enabled, because the launcher has
no resource picker yet.

## Runtime plugin

`native/mods/soundtrack.cpp` registers `nitrogen.soundtrack`. It is linked into
every build that contains the framework's mod runtime, and does nothing unless
an enabled package selects it. During activation it reads the verified
playlist, checks the header, slot order, track numbers 2–11, resource ids and
whole-sector sizes, and appends all ten tracks in one call. Any error leaves the
original soundtrack in place and logs
`nitrogen: soundtrack playlist rejected: <reason>`. Success logs the playlist
and each track's position and length.

## Command line

```sh
python tools/modkit soundtrack status
python tools/modkit soundtrack import 5 my-track.wav
python tools/modkit soundtrack move 5 up
python tools/modkit soundtrack restore 5
python tools/modkit soundtrack details --id me.soundtrack --name "My soundtrack" --version 1.0.0
python tools/modkit soundtrack preview 5
python tools/modkit soundtrack export
python tools/modkit install .local/soundtrack/exports/me.soundtrack-1.0.0.psxmod
```

Validation results are in [mod validation](VALIDATION_MODS.md).

## Licensing

Packages must contain only audio you have the right to distribute. Original
game audio is never exported. The plugin is linked code and is subject to the
pinned framework's PolyForm Noncommercial terms.
