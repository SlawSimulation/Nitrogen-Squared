# Disc import validation

## Multi-BIN CUE import (dev, unreleased) — 2026-11-23

The importer previously refused any CUE with more than one FILE ("Only a
verified single-BIN CUE is supported yet"). The owner's USA dump is a per-track
CUE: 11 BINs, Track 01 MODE2/2352 and Tracks 02–11 AUDIO, each audio BIN
starting with its INDEX 00 pregap (INDEX 01 00:02:00). Until now it could only
be imported through a private merged copy.

`tools/n2o_disc.py` now reads the CUE's metadata instead of assuming one file:

- FILE lines are laid out on the disc in CUE order; INDEX times are relative to
  their FILE, so a track's absolute LBA is its file's start plus its INDEX. This
  is the layout the pinned framework mounts (`iso_reader.cpp`) when no
  PREGAP/POSTGAP lines are present.
- Several tracks may share one BIN, and a BIN may hold part of a track's
  pregap. A track may not continue into a later FILE, which the framework does
  not support.
- Every BIN must resolve relative to the CUE folder (subfolders, either slash
  and a unique letter-case match are accepted), exist, be readable, be a whole
  number of 2,352-byte sectors and be listed once.
- Accepted modes are AUDIO, MODE1/2352 and MODE2/2352. The N2O shape remains one
  data track at LBA 0 followed by audio tracks; ISO reads stop at the data
  track's end, so audio sectors are never read as data.
- Refused with a specific message: non-BINARY files, cooked or other modes,
  PREGAP/POSTGAP (sectors not stored in any BIN), a TRACK before a FILE, an INDEX
  before a TRACK or in another FILE, missing INDEX 01, out-of-order or
  overlapping indexes, misnumbered tracks, a FILE without a TRACK, absolute or
  parent paths and unknown directives. Descriptive metadata (REM, CATALOG,
  TITLE, PERFORMER, SONGWRITER, ISRC, FLAGS) is ignored.

The verified identity in `configs/disc_usa.json` is unchanged. It is the size
and SHA-256 of the disc's sectors in order, which equal the merged BIN's. A
split dump is hashed through a read-only reader across its BINs; nothing is
concatenated on disk and no merged copy is written. `installation.json` keeps
`bin` (the data-track BIN, which the native movie player reads) and adds
`files` with each BIN's start and length. Records without `files` are still read
as one BIN. The soundtrack editor, asset extractor and BIN-to-CUE selection use
the same reader. The framework receives the CUE itself.

`tools/n2o_disc.py` is a compiled-game input, so the fingerprint is now
4dc2fa4b35d2a615d92bfa2e1497eaf75de4bca80789e384997367769fa5eae9. Phase 2's
unreleased change already requires one player rebuild, so the next release
still rebuilds once. No framework file, VERSION, native selection, clock or
TESTING.md change; coverage remains 38.4156% linked / 61.5844% outside.

| Check | Result |
| --- | --- |
| 11 synthetic CUE layout tests: 11-BIN layout equals its merged form, import without merging, reads across BIN boundaries, tracks sharing a BIN, audio never read as data, soundtrack/asset reads equal merged, old records, BIN-to-CUE selection, relative paths and case, 17 refusal cases plus unreadable and partial-sector BINs, changed/missing track BIN | pass |
| Existing single-BIN disc, extractor, soundtrack, portable, media and runtime-path tests (unchanged) | pass |
| Owner's 11-BIN CUE, read only: 11 files at LBAs 0, 13,799 … 195,607; tracks equal the merged layout; size, SHA-256 and executables verify | pass |
| Import from the 11-BIN CUE into a new private folder: executables, seeds, layout and hash identical to the merged import; no BIN written | pass |
| Soundtrack slices and 30 s previews (slots 2, 6, 11), native movie binding header, and all 37 asset-extraction outputs: identical to the merged import | pass |
| Private diagnostics build with the 11-BIN CUE: framework mounted it (NTSC-U, SLUS-00637), opening/title ran, CD trace showed audio track 2 near LBA 14,290 at about 150 s, clean exit. Same result with the merged copy | pass |
| Original files: the hash check confirms contents and the listing (sizes, dates) was unchanged afterwards | pass |

Limits: the synthetic CI runtime does not mount a CUE, so the framework's
multi-file mount is covered by the real-disc run above, not by CI. A per-track
dump whose pregap is stored at the end of the previous BIN with its INDEX 00
under that FILE is refused, because the framework cannot mount it. PREGAP
and POSTGAP lines remain unsupported. Only the verified USA revision imports.
