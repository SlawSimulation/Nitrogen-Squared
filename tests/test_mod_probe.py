"""Original synthetic checks for the trusted-plugin probe; no game data is used."""
import re
import sys
import tempfile
import unittest
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 uses the documented tomli package.
    import tomli as tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import mod_probe  # noqa: E402


class ProbePackageTests(unittest.TestCase):
    def test_manifest_selects_only_the_trusted_plugin(self):
        value = tomllib.loads(mod_probe.manifest("SYNTHETIC"))
        self.assertEqual(value["id"], mod_probe.PACKAGE)
        self.assertEqual(value["target"], [{"game_id": "SYNTHETIC"}])
        self.assertEqual([f["id"] for f in value["feature"]], [mod_probe.FEATURE])
        self.assertFalse(value["feature"][0]["default_enabled"])
        self.assertEqual(value["plugin"], [{"feature": mod_probe.FEATURE, "id": mod_probe.PLUGIN}])
        # The probe must not change game data or behavior.
        for operation in ("patch", "overlay", "resource", "option", "requirement"):
            self.assertNotIn(operation, value)

    def test_state_uses_framework_format_two(self):
        for enabled in (True, False):
            value = tomllib.loads(mod_probe.state(enabled))
            self.assertEqual(value["format_version"], 2)
            self.assertEqual(value["package"], [{"id": mod_probe.PACKAGE, "version": mod_probe.VERSION}])
            self.assertEqual(value["feature"], [{"package_id": mod_probe.PACKAGE, "id": mod_probe.FEATURE,
                                                 "enabled": enabled}])

    def test_variants_and_existing_selection_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp)
            mods = exe / "mods"
            mods.mkdir()
            original = b'format_version = 2\n\n[[feature]]\npackage_id = "other"\nid = "x"\nenabled = true\n'
            (mods / "state.toml").write_bytes(original)
            for variant in mod_probe.VARIANTS:
                package = mod_probe.install(exe, "SYNTHETIC", variant)
                text = (package / "manifest.toml").read_text(encoding="utf-8")
                selection = tomllib.loads((mods / "state.toml").read_text(encoding="utf-8"))
                self.assertEqual(selection["feature"][0]["enabled"], variant != "disabled")
                if variant == "bad-manifest":
                    with self.assertRaises(tomllib.TOMLDecodeError):
                        tomllib.loads(text)
                else:
                    plugin = tomllib.loads(text)["plugin"][0]["id"]
                    self.assertEqual(plugin, mod_probe.MISSING_PLUGIN if variant == "missing-plugin"
                                     else mod_probe.PLUGIN)
                with self.assertRaises(ValueError):
                    mod_probe.install(exe, "SYNTHETIC", variant)
                mod_probe.remove(exe)
                self.assertEqual((mods / "state.toml").read_bytes(), original)
                self.assertFalse((mods / "installed" / mod_probe.PACKAGE).exists())

    def test_remove_without_prior_selection_leaves_no_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp)
            mod_probe.install(exe, "SYNTHETIC")
            mod_probe.remove(exe)
            self.assertFalse((exe / "mods/state.toml").exists())
            with self.assertRaises(ValueError):
                mod_probe.install(exe, "SYNTHETIC", "unknown")

    def test_scenario_expectations_fail_closed(self):
        receipt = {"plugin": mod_probe.PLUGIN, "activations": 1, "vblanks": 3}
        base = dict(name="synthetic", alive=True, exit_code=None, log="", receipt=None,
                    activated=False, refused=False, unavailable=False, ignored=False)
        mod_probe.check(base, "running")
        mod_probe.check(dict(base, receipt=receipt, activated=True), "active")
        mod_probe.check(dict(base, alive=False, exit_code=1, refused=True, unavailable=True), "refused")
        bad = [(dict(base, activated=True), "running"),
               (dict(base, receipt=receipt), "active"),  # receipt without the log line
               (dict(base, receipt=dict(receipt, vblanks=0), activated=True), "active"),
               (dict(base, alive=False, exit_code=0, refused=True, unavailable=True), "refused"),
               (dict(base, alive=False, exit_code=1), "refused")]
        for result, expect in bad:
            with self.assertRaises(RuntimeError):
                mod_probe.check(result, expect)


class ProbeBuildWiringTests(unittest.TestCase):
    def test_option_is_off_by_default_and_isolated(self):
        cmake = (ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
        self.assertRegex(cmake, r'option\(NITROGEN_MODS "[^"]*" OFF\)')
        self.assertIn('if("${PSXRECOMP_ROOT}/runtime/src/mod_runtime.cpp" IN_LIST _nitrogen_sources OR NITROGEN_MODS)\n'
                      "    include(cmake/nitrogen_mods.cmake)\nendif()", cmake)
        module = (ROOT / "cmake/nitrogen_mods.cmake").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'"\$\{CMAKE_CURRENT_SOURCE_DIR\}/([^"]+)"', module),
                         ["native/mods/soundtrack.cpp", "native/mods/mod_probe.cpp"])
        # The probe is development-only: it is linked solely when the option is ON.
        self.assertIn("if(NITROGEN_MODS)\n        target_sources(nitrogen-runtime PRIVATE ${_nitrogen_development_mod_sources})",
                      module)
        # The module only checks that the framework mod runtime is present; it adds no framework source.
        self.assertEqual(re.findall(r"PSXRECOMP_ROOT\}/[^\"]+", module), ["PSXRECOMP_ROOT}/runtime/src/mod_runtime.cpp"])

    def test_plugin_registers_id_without_guest_or_dispatch_changes(self):
        source = (ROOT / "native/mods/mod_probe.cpp").read_text(encoding="utf-8")
        self.assertIn(f'plugin_id = "{mod_probe.PLUGIN}"', source)
        self.assertIn("psx_mod_register_activation_plugin(plugin_id, activate)", source)
        self.assertIn("psx_mod_register_vblank_plugin(plugin_id, vblank)", source)
        for forbidden in ("psx_mod_write", "psx_mod_host_write", "psx_mod_dma_write", "func_override_add",
                          "psx_mod_register_function", "psx_mod_call_guest", "g_psx_ram", "write_word"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
