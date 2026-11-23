/* Original trusted-plugin probe; it reads and writes no guest or game state.
 *
 * Linking this file only registers the "nitrogen.mod-probe" implementation.
 * Its callbacks run only when the framework's committed mod plan selects that
 * id through an enabled package feature. Evidence is printed to stdout and,
 * when NITROGEN_MOD_PROBE_RECEIPT names a file, written there as JSON. */
#include "mod_plugins.h"
#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <string>
#include <system_error>

namespace nitrogen::mods {
namespace {
constexpr const char *plugin_id = "nitrogen.mod-probe";
std::atomic<unsigned> activations{0};
std::atomic<unsigned long long> vblanks{0};
std::atomic<bool> started_seen{false};

void write_receipt(const char *stage)
{
    const char *path = std::getenv("NITROGEN_MOD_PROBE_RECEIPT");
    if (!path || !*path) return;
    const std::filesystem::path target(path), temporary(std::string(path) + ".tmp");
    {
        std::ofstream out(temporary, std::ios::binary | std::ios::trunc);
        if (!out) { std::fprintf(stderr, "nitrogen: mod probe receipt unavailable\n"); return; }
        out << "{\"schema\":1,\"plugin\":\"" << plugin_id << "\",\"stage\":\"" << stage
            << "\",\"activations\":" << activations.load()
            << ",\"vblanks\":" << vblanks.load()
            << ",\"game_started\":" << (started_seen.load() ? "true" : "false") << "}\n";
    }
    std::error_code error;
    std::filesystem::rename(temporary, target, error);
    if (error) std::fprintf(stderr, "nitrogen: mod probe receipt not published: %s\n", error.message().c_str());
}

void activate()
{
    const unsigned count = ++activations;
    std::fprintf(stdout, "nitrogen: mod probe activated (plugin=%s, activation=%u)\n", plugin_id, count);
    std::fflush(stdout);
    write_receipt("activated");
}

// Guest-VBlank callback: host bookkeeping only, so emulation is unaffected.
void vblank()
{
    const unsigned long long count = ++vblanks;
    const bool started = psx_mod_game_started() != 0;
    if (count == 1) {
        std::fprintf(stdout, "nitrogen: mod probe first VBlank callback (game_started=%d)\n", started ? 1 : 0);
        std::fflush(stdout);
        write_receipt("vblank");
    }
    if (started && !started_seen.exchange(true)) {
        std::fprintf(stdout, "nitrogen: mod probe observed game start at VBlank %llu\n", count);
        std::fflush(stdout);
        write_receipt("game-started");
    }
}

struct Install {
    Install()
    {
        if (!psx_mod_register_activation_plugin(plugin_id, activate) ||
            !psx_mod_register_vblank_plugin(plugin_id, vblank))
            std::fprintf(stderr, "nitrogen: mod probe registration failed\n");
    }
} install;
}
}
