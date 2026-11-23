"""GPL-3.0-or-later. Command line for Nitrogen's .psxmod store.

  python tools/modkit list
  python tools/modkit install path/to/package.psxmod
  python tools/modkit enable|disable|remove <package-id>
  python tools/modkit stage          # before a direct run_runtime.py launch
  python tools/modkit plugins        # trusted plugins in the current game build

--store defaults to .local/mods and --work-dir to .local/runtime-dev, the same
locations the portable launcher uses beneath its own folder.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "modkit"

from modkit import ModError  # noqa: E402
from modkit.package import ModStore, runtime_build  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="modkit", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--store", type=Path, default=ROOT / ".local/mods")
    p.add_argument("--work-dir", type=Path, default=ROOT / ".local/runtime-dev")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    sub.add_parser("plugins")
    sub.add_parser("stage")
    sub.add_parser("install").add_argument("archive", type=Path)
    for name in ("enable", "disable", "remove"):
        sub.add_parser(name).add_argument("package_id")
    args = p.parse_args(argv)
    store, build = ModStore(args.store), runtime_build(args.work_dir.resolve())
    try:
        if args.command == "list":
            result = store.status(build)
        elif args.command == "plugins":
            if not build:
                raise ModError("No completed game build in " + str(args.work_dir))
            result = store.plugins(build)
        elif args.command == "install":
            result = store.install(args.archive, build)
        elif args.command in ("enable", "disable"):
            result = store.set_enabled(args.package_id, args.command == "enable", build)
        elif args.command == "remove":
            result = store.remove(args.package_id)
        else:
            if not build:
                raise ModError("No completed game build in " + str(args.work_dir))
            result = store.stage(build[0].parent, build)
    except ModError as e:
        print(f"modkit: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
