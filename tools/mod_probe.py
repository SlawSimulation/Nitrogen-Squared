#!/usr/bin/env python3
"""GPL-3.0-or-later. Install or test the original trusted-plugin probe package.

The probe proves that the pinned framework's own mod catalog, state selection,
plan commit and plugin activation work in a Nitrogen runtime built with
-DNITROGEN_MODS=ON. Packages written here are original text; they contain no
game data, guards or generated code, and select no patch, overlay or resource.

Install beside a private runtime (the directory holding NitrogenSquared-dev):
  python tools/mod_probe.py install --exe-dir <dir> [--variant enabled]
  NITROGEN_MOD_PROBE_RECEIPT=<file> python tools/run_runtime.py ...
  python tools/mod_probe.py remove --exe-dir <dir>

Any existing mods/state.toml is backed up first and restored by remove.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

PACKAGE = "nitrogen.mod-probe"
VERSION = "1.0.0"
FEATURE = "probe"
PLUGIN = "nitrogen.mod-probe"
MISSING_PLUGIN = "nitrogen.mod-probe-unavailable"
BACKUP = "state.toml.mod-probe-backup"
VARIANTS = ("enabled", "disabled", "missing-plugin", "bad-manifest")
ACTIVATED = "nitrogen: mod probe activated"
UNAVAILABLE = "trusted plugin is unavailable"
REFUSED = "cannot launch with selected mods"
IGNORED = "mod manifest ignored"


def manifest(game_id: str, plugin: str = PLUGIN) -> str:
    q = json.dumps
    return (
        "format_version = 10\n"
        f"id = {q(PACKAGE)}\nversion = {q(VERSION)}\n"
        'name = "Nitrogen mod probe"\nauthor = "Nitrogen Squared contributors"\n'
        'description = "Reports trusted-plugin activation. Changes no game behavior."\n'
        'license = "GPL-3.0-or-later"\nresolver = "declarative"\nsave_compatibility = "shared"\n'
        f"\n[[target]]\ngame_id = {q(game_id)}\n"
        f"\n[[feature]]\nid = {q(FEATURE)}\nname = \"Activation probe\"\n"
        'description = "Prints and records activation evidence only."\n'
        'group = "Development"\ndefault_enabled = false\n'
        f"\n[[plugin]]\nfeature = {q(FEATURE)}\nid = {q(plugin)}\n")


def state(enabled: bool) -> str:
    return (f"format_version = 2\n\n[[package]]\nid = {json.dumps(PACKAGE)}\nversion = {json.dumps(VERSION)}\n"
            f"\n[[feature]]\npackage_id = {json.dumps(PACKAGE)}\nid = {json.dumps(FEATURE)}\n"
            f"enabled = {'true' if enabled else 'false'}\n")


def install(exe_dir: Path, game_id: str, variant: str = "enabled") -> Path:
    if variant not in VARIANTS:
        raise ValueError("Unknown probe variant")
    mods = exe_dir.resolve() / "mods"
    package = mods / "installed" / PACKAGE / VERSION
    if (mods / BACKUP).exists() or package.exists():
        raise ValueError("A probe installation already exists; run remove first")
    mods.mkdir(parents=True, exist_ok=True)
    if (mods / "state.toml").exists():
        shutil.copy2(mods / "state.toml", mods / BACKUP)
    package.mkdir(parents=True)
    text = manifest(game_id, MISSING_PLUGIN if variant == "missing-plugin" else PLUGIN)
    if variant == "bad-manifest":
        text = text.replace("[[feature]]", "[[feature]", 1)  # deliberate TOML syntax error
    (package / "manifest.toml").write_text(text, encoding="utf-8", newline="\n")
    (mods / "state.toml").write_text(state(variant != "disabled"), encoding="utf-8", newline="\n")
    return package


def remove(exe_dir: Path):
    mods = exe_dir.resolve() / "mods"
    shutil.rmtree(mods / "installed" / PACKAGE, ignore_errors=True)
    if (mods / BACKUP).exists():
        os.replace(mods / BACKUP, mods / "state.toml")
    elif (mods / "state.toml").exists():
        # No selections existed before install; return to that condition.
        (mods / "state.toml").unlink()


def read_receipt(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def stage_runtime(binary: Path, destination: Path) -> Path:
    """Copy one runtime with its DLLs, assets and staged framework catalog."""
    destination.mkdir(parents=True)
    shutil.copy2(binary, destination / binary.name)
    for extra in [binary.with_suffix(".execution.json"), *binary.parent.glob("*.dll")]:
        if extra.is_file():
            shutil.copy2(extra, destination / extra.name)
    for folder in ("assets", "bios", "psx_execution", "mods/bundled"):
        if (binary.parent / folder).is_dir():
            shutil.copytree(binary.parent / folder, destination / folder)
    return destination / binary.name


def launch(binary: Path, config: Path, disc: Path, receipt: Path, seconds: float, wait_for=None):
    """Run unattended until exit, the wanted receipt stage, or the deadline."""
    env = os.environ.copy()
    env["NITROGEN_MOD_PROBE_RECEIPT"] = str(receipt)
    env.setdefault("SDL_AUDIODRIVER", "dummy")
    logs = binary.parent
    with (logs / "probe-stdout.txt").open("wb") as out, (logs / "probe-stderr.txt").open("wb") as err:
        process = subprocess.Popen([str(binary), "--game", str(config), "--disc", str(disc), "--headless"],
                                   cwd=binary.parent, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
        deadline = time.monotonic() + seconds
        while process.poll() is None and time.monotonic() < deadline:
            value = read_receipt(receipt) if wait_for else None
            if value and value.get("stage") in wait_for:
                break
            time.sleep(0.2)
        alive = process.poll() is None
        if alive:
            process.kill()
        process.wait(timeout=30)
    text = (logs / "probe-stdout.txt").read_text(errors="replace") + (logs / "probe-stderr.txt").read_text(errors="replace")
    return alive, process.returncode, text


def scenario(binary: Path, work: Path, config: Path, disc: Path, game_id: str, name: str,
             variant: str | None, seconds: float | None = None):
    """Run one staged scenario and return its observations (no assertions).

    An enabled probe stops as soon as its VBlank receipt appears; scenarios
    expected to keep running are observed for a shorter bounded interval."""
    seconds = seconds or (30.0 if variant == "enabled" else 12.0)
    staged = stage_runtime(binary, Path(tempfile.mkdtemp(prefix=f"mod-probe-{name}-", dir=work)) / "runtime")
    if variant:
        install(staged.parent, game_id, variant)
    receipt = staged.parent / "probe-receipt.json"
    wait = {"vblank", "game-started"} if variant == "enabled" else None
    alive, code, text = launch(staged, config, disc, receipt, seconds, wait)
    if variant:
        remove(staged.parent)
    return dict(name=name, alive=alive, exit_code=None if alive else code, log=text,
                receipt=read_receipt(receipt), activated=ACTIVATED in text,
                refused=REFUSED in text, unavailable=UNAVAILABLE in text, ignored=IGNORED in text)


def check(result: dict, expect: str):
    """expect: running (stays up, no probe), active (receipt), refused (fails closed)."""
    receipt, name = result["receipt"], result["name"]
    if expect == "active":
        good = (receipt and receipt.get("plugin") == PLUGIN and receipt.get("activations") == 1 and
                receipt.get("vblanks", 0) >= 1 and result["activated"] and not result["refused"])
    elif expect == "running":
        good = result["alive"] and receipt is None and not result["activated"] and not result["refused"]
    elif expect == "refused":
        good = (not result["alive"] and result["exit_code"] != 0 and result["refused"] and
                result["unavailable"] and receipt is None and not result["activated"])
    else:
        raise ValueError("Unknown expectation")
    if not good:
        raise RuntimeError(f"Mod probe scenario {name} did not meet '{expect}':\n" +
                           json.dumps({k: v for k, v in result.items() if k != "log"}, indent=2) +
                           "\n" + result["log"][-6000:])
    summary = f"receipt={json.dumps(receipt)}" if receipt else f"alive={result['alive']} exit={result['exit_code']}"
    print(f"Mod probe scenario {name} passed ({expect}): {summary}", flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    i = sub.add_parser("install")
    i.add_argument("--exe-dir", type=Path, required=True)
    i.add_argument("--game-id", default="SLUS-00637")
    i.add_argument("--variant", choices=VARIANTS, default="enabled")
    r = sub.add_parser("remove")
    r.add_argument("--exe-dir", type=Path, required=True)
    v = sub.add_parser("verify")
    v.add_argument("--receipt", type=Path, required=True)
    args = p.parse_args()
    if args.command == "install":
        print(f"Installed {install(args.exe_dir, args.game_id, args.variant)} ({args.variant})")
    elif args.command == "remove":
        remove(args.exe_dir)
        print("Removed the probe package; prior state.toml restored when present")
    else:
        value = read_receipt(args.receipt)
        if not value or value.get("plugin") != PLUGIN or value.get("activations", 0) < 1:
            print("No activation receipt", file=sys.stderr)
            return 1
        print(json.dumps(value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
