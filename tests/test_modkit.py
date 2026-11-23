"""Original synthetic .psxmod store, validation and runtime-staging checks.

No game data is used. The framework audit is stubbed here with output in the
pinned runtime's exact --audit-mod-plugins format; tools/ci_runtime_build.py
exercises the same code against a real linked runtime."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 uses the documented tomli package.
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from modkit import ModError, GAME_ID  # noqa: E402
from modkit import manifest, validator  # noqa: E402
from modkit.package import ModStore, RECEIPT, runtime_build, state_text  # noqa: E402

PROBE = "nitrogen.mod-probe"
FRAMEWORK = ["psx.8mb-ram", "psx.bezel", "psx.cd-speed", "psx.crt-filter", "psx.enhanced-audio", "psx.fast-loading",
             "psx.hd-textures", "psx.image-filtering", "psx.internal-resolution", "psx.pgxp", "psx.rewind"]


def manifest_text(package_id="example.synthetic", version="1.0.0", plugins=(PROBE,), game=GAME_ID, extra=""):
    lines = ["format_version = 10", f'id = "{package_id}"', f'version = "{version}"', 'name = "Synthetic Package"',
             'author = "Synthetic author"', 'description = "Original synthetic test package."',
             'license = "GPL-3.0-or-later"', extra, "", "[[target]]", f'game_id = "{game}"', "",
             "[[feature]]", 'id = "main"', 'name = "Main"', "default_enabled = false"]
    for plugin in plugins:
        lines += ["", "[[plugin]]", 'feature = "main"', f'id = "{plugin}"']
    return "\n".join(lines) + "\n"


def fake_audit(registered):
    """Stand-in for the runtime's --audit-mod-plugins output (format from main.cpp)."""
    def audit(executable, roots):
        errors, declared, manifests = [], set(), 0
        for root in roots:
            path = Path(root) / "manifest.toml"
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            if 'resolver = "bogus"' in text:
                errors.append(f"{path}: {path}: resolver must be declarative or builtin:<id>")
                continue
            manifests += 1
            declared |= {p["id"] for p in tomllib.loads(text).get("plugin", [])}
        lines = [f"mod plugin audit: {manifests} manifest(s), {len(declared)} declared id(s), {len(registered)} registered id(s)"]
        lines += [f"  manifest error: {e}" for e in errors]
        lines += [f"  UNDECLARED (can never run): {p}" for p in registered if p not in declared]
        lines.append("mod plugin audit: " + ("PASS" if not errors else "FAIL"))
        return "\n".join(lines) + "\n"
    return audit


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ModStore(self.root / ".local/mods")
        self.exe_dir = self.root / ".local/runtime-dev/build"
        self.exe_dir.mkdir(parents=True)
        (self.exe_dir / "game.exe").write_bytes(b"synthetic")
        self.with_plugins([PROBE, *FRAMEWORK])

    def tearDown(self):
        patch.stopall()
        self.tmp.cleanup()

    def with_plugins(self, registered, digest="on"):
        patch.stopall()
        patch("modkit.validator._audit", side_effect=fake_audit(registered)).start()
        self.build = (self.exe_dir / "game.exe", "sha-" + digest)

    def archive(self, name="pkg", text="", files=None):
        """text None writes no root manifest; empty text means the default manifest."""
        path = self.root / f"{name}.psxmod"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            if text is not None:
                z.writestr("manifest.toml", text or manifest_text())
            for member, data in (files or {}).items():
                z.writestr(member, data)
        return path


class ManifestTests(unittest.TestCase):
    def test_framework_identity_rules(self):
        for good in ("a", "nitrogen.mod-probe", "x_1-2.y"):
            self.assertTrue(manifest.valid_id(good), good)
        for bad in ("", "Upper", ".lead", "trail.", "has space", "a/b", "x" * 97):
            self.assertFalse(manifest.valid_id(bad), bad)
        for good in ("1.0.0", "10.20.30", "1.0.0-beta.1"):
            self.assertTrue(manifest.valid_version(good))
        for bad in ("1.0", "1.0.0.0", "v1.0.0", "1.0.0-", "1.0.0-a/b", "1.0.0-..\\x"):
            self.assertFalse(manifest.valid_version(bad), bad)

    def test_summary_and_refusals(self):
        info = manifest.parse(manifest_text())
        self.assertEqual((info.id, info.version, info.author, info.license), ("example.synthetic", "1.0.0",
                                                                               "Synthetic author", "GPL-3.0-or-later"))
        self.assertEqual([p["id"] for p in info.plugins], [PROBE])
        self.assertTrue(info.targets_game(GAME_ID))
        for text, message in [("format_version = 10\nid = 'x'\n[[feature]", "not valid TOML"),
                              (manifest_text().replace("format_version = 10", "format_version = 12"), "format_version"),
                              (manifest_text().replace("format_version = 10", 'format_version = "10"'), "format_version"),
                              (manifest_text(package_id="Bad.Id"), "package id"),
                              (manifest_text(version="1.0"), "version"),
                              (manifest_text().replace('name = "Synthetic Package"', 'name = " "'), "name is empty")]:
            with self.subTest(message=message), self.assertRaisesRegex(ModError, message):
                manifest.parse(text)


class ArchiveTests(StoreCase):
    def test_unsafe_and_malformed_archives_are_refused_before_extraction(self):
        cases = {"no manifest": self.archive("nomanifest", None, files={"README.txt": "x"}),
                 "nested manifest": self.archive("nested", None, files={"folder/manifest.toml": manifest_text()}),
                 "parent path": self.archive("parent", files={"../escape.txt": "x"}),
                 "absolute path": self.archive("absolute", files={"/abs.txt": "x"}),
                 "drive path": self.archive("drive", files={"C:/drive.txt": "x"}),
                 "dot path": self.archive("dot", files={"a/./b.txt": "x"}),
                 "duplicate": self.archive("dupe", files={"Read.txt": "a", "read.TXT": "b"})}
        cases["not zip"] = self.root / "plain.psxmod"
        cases["not zip"].write_text("not an archive")
        cases["extension"] = self.root / "package.zip"
        shutil.copy(self.archive("ext"), cases["extension"])
        many = self.root / "many.psxmod"
        with zipfile.ZipFile(many, "w") as z:
            z.writestr("manifest.toml", manifest_text())
            for i in range(validator.MAX_ENTRIES):
                z.writestr(f"f{i}", "")
        cases["entry count"] = many
        for label, path in cases.items():
            with self.subTest(label=label), self.assertRaises(ModError):
                self.store.install(path, self.build)
        self.assertEqual(self.store.records(), [])
        self.assertFalse(any((self.store.root / "packages").glob("*")))
        scratch = self.store.root / "tmp"
        self.assertEqual(list(scratch.iterdir()) if scratch.exists() else [], [])
        self.assertFalse((self.root / "escape.txt").exists())

    def test_expanded_size_limit(self):
        path = self.archive()
        with patch.object(validator, "MAX_BYTES", 64):
            with self.assertRaisesRegex(ModError, "256 MiB"):
                validator.inspect_archive(path)


class InstallTests(StoreCase):
    def test_valid_package_installs_disabled_with_metadata(self):
        path = self.archive(files={"README.txt": "hello", "data/notes.txt": "x"})
        record = self.store.install(path, self.build)
        self.assertEqual(record["validation"], "framework")
        self.assertEqual(record["files"], 3)
        self.assertEqual(record["archive"]["size"], path.stat().st_size)
        installed = self.store.root / "packages/example.synthetic/1.0.0"
        self.assertEqual((installed / "data/notes.txt").read_text(), "x")
        meta = json.loads((self.store.root / "metadata/example.synthetic/1.0.0.json").read_text())
        self.assertEqual(meta["archive"]["sha256"], record["archive"]["sha256"])
        [status] = self.store.status(self.build)
        self.assertFalse(status["enabled"])
        self.assertEqual(status["compatibility"]["state"], "ready")
        self.assertEqual(status["required_plugins"], [PROBE])

    def test_framework_refusal_and_without_build(self):
        bad = self.archive("bad", manifest_text(extra='resolver = "bogus"'))
        with self.assertRaisesRegex(ModError, "resolver must be declarative or builtin:<id>$"):
            self.store.install(bad, self.build)
        self.assertEqual(self.store.records(), [])
        record = self.store.install(bad, None)  # No game build yet: basic checks only.
        self.assertEqual(record["validation"], "basic")
        [status] = self.store.status(self.build)
        self.assertEqual(status["compatibility"]["state"], "invalid")
        self.assertIn("resolver must be", status["compatibility"]["detail"])
        with self.assertRaisesRegex(ModError, "cannot be enabled"):
            self.store.set_enabled("example.synthetic", True, self.build)

    def test_duplicate_package_ids(self):
        self.store.install(self.archive("one"), self.build)
        with self.assertRaisesRegex(ModError, "already installed"):
            self.store.install(self.archive("again"), self.build)
        with self.assertRaisesRegex(ModError, "version 1.0.0.*Remove it before installing version 2.0.0"):
            self.store.install(self.archive("two", manifest_text(version="2.0.0")), self.build)
        self.assertEqual([r["version"] for r in self.store.records()], ["1.0.0"])


class SelectionTests(StoreCase):
    def test_enable_disable_remove(self):
        self.store.install(self.archive(), self.build)
        self.store.set_enabled("example.synthetic", True, self.build)
        self.assertEqual(json.loads((self.store.root / "selections.json").read_text())["enabled"], ["example.synthetic"])
        self.store.set_enabled("example.synthetic", False, self.build)
        self.assertEqual(self.store.enabled_ids(), set())
        self.store.set_enabled("example.synthetic", True, self.build)
        self.store.remove("example.synthetic")
        self.assertEqual((self.store.records(), self.store.enabled_ids()), ([], set()))
        self.assertFalse((self.store.root / "packages/example.synthetic").exists())
        self.assertFalse((self.store.root / "metadata/example.synthetic").exists())
        with self.assertRaisesRegex(ModError, "not installed"):
            self.store.remove("example.synthetic")

    def test_missing_plugin_other_game_and_unchecked(self):
        self.store.install(self.archive("a", manifest_text("example.missing", plugins=("example.absent",))), self.build)
        self.store.install(self.archive("b", manifest_text("example.other", game="SLUS-99999")), self.build)
        self.store.install(self.archive("c", manifest_text("example.generic", game="*", plugins=("psx.bezel",))), self.build)
        states = {r["id"]: r for r in self.store.status(self.build)}
        self.assertEqual(states["example.missing"]["compatibility"]["state"], "missing-plugins")
        self.assertEqual(states["example.missing"]["missing_plugins"], ["example.absent"])
        self.assertEqual(states["example.other"]["compatibility"]["state"], "other-game")
        self.assertEqual(states["example.generic"]["compatibility"]["state"], "ready")
        for package in ("example.missing", "example.other"):
            with self.subTest(package=package), self.assertRaises(ModError):
                self.store.set_enabled(package, True, self.build)
        self.assertEqual({r["compatibility"]["state"] for r in self.store.status(None)}, {"unchecked", "other-game"})
        self.store.set_enabled("example.missing", True, None)  # Allowed, but checked again at launch.
        with self.assertRaisesRegex(ModError, "lacks example.absent"):
            self.store.stage(self.exe_dir, self.build)

    def test_mods_off_build_reports_probe_missing(self):
        self.store.install(self.archive(), self.build)
        self.store.set_enabled("example.synthetic", True, self.build)
        self.with_plugins(FRAMEWORK, digest="off")  # Same store, a NITROGEN_MODS=OFF build.
        [status] = self.store.status(self.build)
        self.assertEqual(status["missing_plugins"], [PROBE])
        with self.assertRaisesRegex(ModError, "Disable it in Mods"):
            self.store.stage(self.exe_dir, self.build)
        self.assertFalse((self.exe_dir / "mods").exists())

    def test_plugin_report_is_cached_per_executable_and_checked(self):
        self.store.plugins(self.build)
        self.assertEqual(json.loads((self.store.root / "runtime-plugins.json").read_text())["executable_sha256"], "sha-on")
        with patch("modkit.validator._audit", side_effect=AssertionError("cached")):
            self.assertIn(PROBE, self.store.plugins(self.build))
        with patch("modkit.validator._audit", return_value="mod plugin audit: 0 manifest(s), 0 declared id(s), 2 registered id(s)\n"
                   "  UNDECLARED (can never run): psx.bezel\n"):
            with self.assertRaisesRegex(ModError, "incomplete"):
                validator.registered_plugins(self.build[0])
        with patch("modkit.validator._audit", side_effect=ModError("did not report")):
            with self.assertRaises(ModError):
                self.store.plugins((self.build[0], "sha-new"))


class StagingTests(StoreCase):
    def enable(self, *packages):
        for package in packages:
            self.store.install(self.archive(package, manifest_text(package)), self.build)
            self.store.set_enabled(package, True, self.build)

    def test_nothing_enabled_and_never_staged_changes_nothing(self):
        self.store.install(self.archive(), self.build)  # Installed but disabled.
        with patch("modkit.validator._audit", side_effect=AssertionError("no build check without mods")):
            self.assertEqual(self.store.stage(self.exe_dir, self.build), dict(changed=False, packages=[]))
        self.assertFalse((self.exe_dir / "mods").exists())

    def test_stage_writes_framework_layout_and_state(self):
        bundled = self.exe_dir / "mods/bundled/psx.enhancement.pgxp/1.0.0"
        bundled.mkdir(parents=True)
        (bundled / "manifest.toml").write_text("framework owned")
        (self.exe_dir / "mods/state.toml").write_text("format_version = 2\n# earlier runtime choices\n")
        self.enable("example.one", "example.two")
        result = self.store.stage(self.exe_dir, self.build)
        self.assertEqual([p["id"] for p in result["packages"]], ["example.one", "example.two"])
        mods = self.exe_dir / "mods"
        for package in ("example.one", "example.two"):
            self.assertEqual((mods / f"installed/{package}/1.0.0/manifest.toml").read_text(),
                             (self.store.root / f"packages/{package}/1.0.0/manifest.toml").read_text())
        state = tomllib.loads((mods / "state.toml").read_text())
        self.assertEqual(state["format_version"], 2)
        self.assertEqual(state["package"], [{"id": "example.one", "version": "1.0.0"}, {"id": "example.two", "version": "1.0.0"}])
        self.assertEqual(state["feature"], [{"package_id": "example.one", "id": "main", "enabled": True},
                                            {"package_id": "example.two", "id": "main", "enabled": True}])
        self.assertEqual((bundled / "manifest.toml").read_text(), "framework owned")
        receipt = json.loads((mods / RECEIPT).read_text())
        self.assertTrue(receipt["backup"].startswith("state.toml.before-nitrogen-mods-"))
        self.assertIn("earlier runtime choices", (mods / receipt["backup"]).read_text())
        self.assertEqual(list(self.store.staging_dir.iterdir()), [])

        # Disabling one package and staging again drops only that package.
        self.store.set_enabled("example.two", False, self.build)
        self.store.stage(self.exe_dir, self.build)
        self.assertFalse((mods / "installed/example.two").exists())
        self.assertEqual([p["id"] for p in tomllib.loads((mods / "state.toml").read_text())["package"]], ["example.one"])

        # With nothing enabled the earlier condition returns, including the prior state.toml.
        self.store.set_enabled("example.one", False, self.build)
        self.assertEqual(self.store.stage(self.exe_dir, self.build)["changed"], True)
        self.assertEqual(list((mods / "installed").iterdir()), [])
        self.assertIn("earlier runtime choices", (mods / "state.toml").read_text())
        self.assertFalse((mods / RECEIPT).exists())
        self.assertEqual(self.store.stage(self.exe_dir, self.build)["changed"], False)

    def test_packages_persist_after_rebuild_and_restage(self):
        self.enable("example.one")
        self.store.stage(self.exe_dir, self.build)
        staged = (self.exe_dir / "mods/installed/example.one/1.0.0/manifest.toml").read_bytes()
        shutil.rmtree(self.root / ".local/runtime-dev")  # A rebuild replaces the whole work directory.
        self.exe_dir.mkdir(parents=True)
        (self.exe_dir / "game.exe").write_bytes(b"rebuilt")
        self.with_plugins([PROBE, *FRAMEWORK], digest="rebuilt")
        self.assertEqual([(r["id"], r["enabled"]) for r in self.store.status(self.build)], [("example.one", True)])
        self.store.stage(self.exe_dir, self.build)
        self.assertEqual((self.exe_dir / "mods/installed/example.one/1.0.0/manifest.toml").read_bytes(), staged)

    def test_foreign_installed_packages_are_never_deleted(self):
        foreign = self.exe_dir / "mods/installed/someone.else/1.0.0"
        foreign.mkdir(parents=True)
        (foreign / "manifest.toml").write_text("player owned")
        self.enable("example.one")
        with self.assertRaisesRegex(ModError, "did not install.*someone.else/1.0.0"):
            self.store.stage(self.exe_dir, self.build)
        self.assertEqual((foreign / "manifest.toml").read_text(), "player owned")
        self.assertFalse((self.exe_dir / "mods/state.toml").exists())

    def test_staging_without_a_build_is_refused(self):
        self.enable("example.one")
        with self.assertRaisesRegex(ModError, "Prepare the game"):
            self.store.stage(self.exe_dir, None)

    def test_state_text_matches_phase_zero_format(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import mod_probe
        record = dict(id=mod_probe.PACKAGE, version=mod_probe.VERSION, features=[dict(id=mod_probe.FEATURE)])
        self.assertEqual(tomllib.loads(state_text([record])), tomllib.loads(mod_probe.state(True)))


class RuntimeBuildTests(unittest.TestCase):
    def test_runtime_build_reads_only_completed_builds(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            self.assertIsNone(runtime_build(work))
            exe = work / "build/game.exe"
            exe.parent.mkdir()
            exe.write_bytes(b"x")
            record = dict(build_succeeded=False, executable=str(exe), build_identity=dict(executable_sha256="abc"))
            (work / "runtime-build.json").write_text(json.dumps(record))
            self.assertIsNone(runtime_build(work))
            record["build_succeeded"] = True
            (work / "runtime-build.json").write_text(json.dumps(record))
            self.assertEqual(runtime_build(work), (exe, "abc"))
            exe.unlink()
            self.assertIsNone(runtime_build(work))

    def test_reason_strips_repeated_paths(self):
        message = r"C:\x y\manifest.toml: C:\x y\manifest.toml: resolver must be declarative or builtin:<id>"
        self.assertEqual(validator.reason(message), "resolver must be declarative or builtin:<id>")
        self.assertEqual(validator.reason("plain"), "plain")


class LauncherIntegrationTests(StoreCase):
    def setUp(self):
        super().setUp()
        from launcher_service import Service
        from linux_support import LinuxLayout
        self.layout = LinuxLayout(self.root)
        self.layout.app = ROOT
        self.layout.writable()
        self.events = []
        self.service = Service(self.layout, self.events.append, testing=True)

    def record_build(self):
        record = dict(build_succeeded=True, executable=str(self.exe_dir / "game.exe"),
                      build_identity=dict(executable_sha256=self.build[1]))
        (self.layout.work / "runtime-build.json").write_text(json.dumps(record))

    def wait_idle(self):
        import time
        deadline = time.monotonic() + 10
        while self.service.busy and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.service.busy)

    def test_service_commands_use_the_durable_store(self):
        self.assertEqual(self.service.mods.root, self.layout.local / "mods")
        listing = self.service.handle("mods", {})
        self.assertEqual((listing["packages"], listing["checked"]), ([], False))
        for args in ({}, {"path": 7}, {"path": str(self.root / "package.zip")}):
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, "psxmod"):
                self.service.handle("mod_install", args)
        self.record_build()
        self.service.handle("mod_install", {"path": str(self.archive())})
        self.wait_idle()
        self.assertEqual(self.service.title, "Installed Synthetic Package 1.0.0. Enable it in Mods.")
        listing = self.service.handle("mods", {})
        self.assertTrue(listing["checked"])
        self.assertEqual([(p["id"], p["compatibility"]["state"]) for p in listing["packages"]], [("example.synthetic", "ready")])
        revision = self.service.snapshot()["mods_revision"]
        self.service.handle("mod_enable", {"id": "example.synthetic", "enabled": True})
        self.assertGreater(self.service.snapshot()["mods_revision"], revision)
        self.assertTrue(self.service.handle("mods", {})["packages"][0]["enabled"])
        for args in ({"id": "example.synthetic"}, {"id": "example.synthetic", "enabled": "yes"}, {"id": 3, "enabled": True}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.service.handle("mod_enable", args)
        self.service.busy, self.service.phase = True, "running"
        for command, args in (("mod_enable", {"id": "example.synthetic", "enabled": False}),
                              ("mod_remove", {"id": "example.synthetic"}),
                              ("mod_install", {"path": str(self.archive("other"))})):
            with self.subTest(command=command), self.assertRaisesRegex(ValueError, "Wait for the current task"):
                self.service.handle(command, args)
        self.assertTrue(self.service.handle("mods", {})["checked"])  # A running game's build is still checked.
        self.service.busy, self.service.phase = False, "idle"
        with patch.object(self.service, "open_path") as opened:
            self.service.handle("open", {"target": "mods"})
        opened.assert_called_once_with(self.layout.local / "mods")
        self.service.handle("mod_remove", {"id": "example.synthetic"})
        self.assertEqual(self.service.handle("mods", {})["packages"], [])

    def test_play_stages_enabled_packages_before_launch(self):
        from portable_launcher import Backend
        messages = []
        backend = Backend(self.layout, lambda kind, text: messages.append(text))
        backend.stage_mods()  # No build recorded: nothing to stage, nothing created.
        self.assertFalse((self.exe_dir / "mods").exists())
        self.record_build()
        backend.stage_mods()  # Build present but nothing enabled: unchanged launch.
        self.assertFalse((self.exe_dir / "mods").exists())
        self.service.mods.install(self.archive(), self.build)
        self.service.mods.set_enabled("example.synthetic", True, self.build)
        backend.stage_mods()
        self.assertTrue((self.exe_dir / "mods/installed/example.synthetic/1.0.0/manifest.toml").is_file())
        self.assertIn("Mods enabled: example.synthetic 1.0.0", messages)
        self.service.mods.set_enabled("example.synthetic", False, self.build)
        backend.stage_mods()
        self.assertFalse((self.exe_dir / "mods/state.toml").exists())
        self.assertIn("No mods enabled; removed earlier mod selections from the game build.", messages)

    def test_play_refuses_an_incompatible_enabled_package(self):
        from portable_launcher import Backend
        self.record_build()
        self.service.mods.install(self.archive(), self.build)
        self.service.mods.set_enabled("example.synthetic", True, self.build)
        self.with_plugins(FRAMEWORK, digest="off")
        self.record_build()  # A rebuilt executable has a new identity, so its plugins are re-read.
        backend = Backend(self.layout, lambda kind, text: None)
        with patch.object(backend, "prepare"), patch("portable_launcher.reusable_runtime", return_value=True),                 patch("portable_launcher.run") as run:
            self.layout.state = lambda: dict(prepared_version="x", root=str(self.layout.root),
                                             prepared_tools=self.layout.tool_fingerprint())
            with self.assertRaisesRegex(ModError, "lacks nitrogen.mod-probe"):
                backend.play()
        run.assert_not_called()  # The game is never started with an unresolvable plan.


class CommandLineTests(StoreCase):
    def test_cli_install_list_enable_and_errors(self):
        from modkit.__main__ import main
        work = self.root / "nowhere"
        args = ["--store", str(self.store.root), "--work-dir", str(work)]
        with patch("sys.stdout"), patch("sys.stderr"):
            self.assertEqual(main([*args, "install", str(self.archive())]), 0)
            self.assertEqual(main([*args, "enable", "example.synthetic"]), 0)
            self.assertEqual(main([*args, "list"]), 0)
            self.assertEqual(main([*args, "stage"]), 1)  # No completed build.
            self.assertEqual(main([*args, "remove", "missing.id"]), 1)
        self.assertEqual(self.store.enabled_ids(), {"example.synthetic"})


if __name__ == "__main__":
    unittest.main()
