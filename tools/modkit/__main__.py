"""GPL-3.0-or-later. Command line for Nitrogen's .psxmod store and soundtrack editor.

  python tools/modkit list
  python tools/modkit install path/to/package.psxmod
  python tools/modkit enable|disable|remove <package-id>
  python tools/modkit stage          # before a direct run_runtime.py launch
  python tools/modkit plugins        # trusted plugins in the current game build

  python tools/modkit soundtrack status
  python tools/modkit soundtrack import <slot 2-11> <file.wav>
  python tools/modkit soundtrack restore <slot> | move <slot> up|down | reset
  python tools/modkit soundtrack details --id ID --name NAME --version 1.0.0 [--author A] [--license L]
  python tools/modkit soundtrack preview [<slot>]   # writes a preview WAVE and prints its path
  python tools/modkit soundtrack export             # validated .psxmod in the project's exports folder

--store defaults to .local/mods, --work-dir to .local/runtime-dev, --project to
.local/soundtrack and --installation to .local/installation: the same locations
the portable launcher uses beneath its own folder.
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
from modkit.soundtrack import SoundtrackProject  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def soundtrack(args, build):
    project = SoundtrackProject(args.project, args.installation)
    action = args.action
    if action == "status":
        return project.status()
    if action == "import":
        return project.import_wave(int(args.values[0]), Path(args.values[1]))
    if action == "restore":
        project.restore(int(args.values[0]))
    elif action == "move":
        project.move(int(args.values[0]), {"up": -1, "down": 1}[args.values[1]])
    elif action == "reset":
        project.reset()
    elif action == "details":
        return project.set_details({k: v for k, v in vars(args).items()
                                    if k in ("id", "name", "version", "author", "license", "description") and v is not None})
    elif action == "preview":
        return str(project.preview(int(args.values[0]) if args.values else None))
    elif action == "export":
        return project.export(build)
    return project.status()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="modkit", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--store", type=Path, default=ROOT / ".local/mods")
    p.add_argument("--work-dir", type=Path, default=ROOT / ".local/runtime-dev")
    p.add_argument("--project", type=Path, default=ROOT / ".local/soundtrack")
    p.add_argument("--installation", type=Path, default=ROOT / ".local/installation")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    sub.add_parser("plugins")
    sub.add_parser("stage")
    sub.add_parser("install").add_argument("archive", type=Path)
    for name in ("enable", "disable", "remove"):
        sub.add_parser(name).add_argument("package_id")
    s = sub.add_parser("soundtrack")
    s.add_argument("action", choices=["status", "import", "restore", "move", "reset", "details", "preview", "export"])
    s.add_argument("values", nargs="*")
    for key in ("id", "name", "version", "author", "license", "description"):
        s.add_argument("--" + key)
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
        elif args.command == "soundtrack":
            result = soundtrack(args, build)
        else:
            if not build:
                raise ModError("No completed game build in " + str(args.work_dir))
            result = store.stage(build[0].parent, build)
    except (ModError, IndexError, KeyError, ValueError) as e:
        print(f"modkit: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
