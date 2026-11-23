/* Original trusted plugin "nitrogen.soundtrack": a replacement CD-DA playlist.
 *
 * It runs only when an enabled package feature selects this id. During the
 * activation callback it reads the feature's verified "playlist" resource and
 * builds exactly ten audio tracks (N2O's disc tracks 2-11) with the framework's
 * psx_mod_append_cdda_tracks(). Each slot is either an original audio track on
 * the player's mounted disc or a verified resource of raw 2352-byte CD-DA
 * sectors. The native CD controller still performs playback, volume, reports
 * and save states; this file reads and writes no guest memory.
 *
 * Playlist resource (UTF-8 text, LF or CRLF):
 *   nitrogen-soundtrack 1
 *   2 disc 2
 *   3 resource audio-1
 *   ... one line per slot, 2 through 11 in order ...
 * Any error leaves the original soundtrack in place and is reported. */
#include "mod_plugins.h"
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <sstream>
#include <string>
#include <vector>

namespace nitrogen::mods {
namespace {
constexpr const char *plugin_id = "nitrogen.soundtrack";
constexpr uint32_t first_slot = 2, last_slot = 11, slots = last_slot - first_slot + 1;
constexpr uint32_t sector_bytes = 2352;
constexpr size_t max_playlist_bytes = 4096;

bool valid_resource_id(const std::string &id)
{
    if (id.empty() || id.size() > 96 || id.front() == '.' || id.back() == '.') return false;
    for (const unsigned char c : id)
        if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '.' || c == '-' || c == '_')) return false;
    return true;
}

void reject(const std::string &reason)
{
    std::fprintf(stderr, "nitrogen: soundtrack playlist rejected: %s; the original soundtrack is kept\n", reason.c_str());
    std::fflush(stderr);
}

void activate()
{
    const uint8_t *bytes = nullptr;
    uint64_t size = 0;
    if (!psx_mod_current_resource_bytes("playlist", &bytes, &size)) return reject("no verified playlist resource");
    if (!size || size > max_playlist_bytes) return reject("playlist size is out of range");
    std::istringstream text(std::string(reinterpret_cast<const char *>(bytes), size_t(size)));
    std::string line;
    if (!std::getline(text, line)) return reject("playlist is empty");
    if (!line.empty() && line.back() == '\r') line.pop_back();
    if (line != "nitrogen-soundtrack 1") return reject("unsupported playlist header");

    std::vector<std::string> ids(slots);   // keeps resource_id strings alive for the call
    std::vector<PSXModCDDATrack> tracks(slots);
    std::string summary;
    for (uint32_t i = 0; i < slots; ++i) {
        if (!std::getline(text, line)) return reject("playlist has fewer than ten slots");
        if (!line.empty() && line.back() == '\r') line.pop_back();
        std::istringstream fields(line);
        uint32_t slot = 0;
        std::string kind, value, extra;
        if (!(fields >> slot >> kind >> value) || (fields >> extra) || slot != first_slot + i)
            return reject("malformed line for slot " + std::to_string(first_slot + i));
        PSXModCDDATrack &track = tracks[i];
        if (kind == "disc") {
            char *end = nullptr;
            const unsigned long number = std::strtoul(value.c_str(), &end, 10);
            if (!end || *end || number < first_slot || number > last_slot)
                return reject("slot " + std::to_string(slot) + " names a track outside 2-11");
            track = {nullptr, 0, 0, uint32_t(number)};
        } else if (kind == "resource") {
            if (!valid_resource_id(value)) return reject("slot " + std::to_string(slot) + " has an invalid resource id");
            const uint8_t *audio = nullptr;
            uint64_t audio_size = 0;
            if (!psx_mod_current_resource_bytes(value.c_str(), &audio, &audio_size))
                return reject("resource " + value + " is not a verified resource of this feature");
            if (!audio_size || audio_size % sector_bytes || audio_size / sector_bytes > 0xFFFFFFFFull)
                return reject("resource " + value + " is not whole 2352-byte CD-DA sectors");
            ids[i] = value;
            track = {ids[i].c_str(), 0, uint32_t(audio_size / sector_bytes), 0};
        } else {
            return reject("slot " + std::to_string(slot) + " has an unknown source");
        }
        summary += " " + std::to_string(slot) + "=" + kind + ":" + value;
    }
    while (std::getline(text, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (!line.empty()) return reject("playlist has more than ten slots");
    }
    uint32_t lbas[slots] = {}, counts[slots] = {};
    if (!psx_mod_append_cdda_tracks(tracks.data(), slots, lbas, counts))
        return reject("the framework refused the CD-DA playlist");
    std::fprintf(stdout, "nitrogen: soundtrack playlist applied:%s\n", summary.c_str());
    for (uint32_t i = 0; i < slots; ++i)
        std::fprintf(stdout, "nitrogen: soundtrack track %u lba %u sectors %u\n", first_slot + i, lbas[i], counts[i]);
    std::fflush(stdout);
}

struct Install {
    Install()
    {
        if (!psx_mod_register_activation_plugin(plugin_id, activate))
            std::fprintf(stderr, "nitrogen: soundtrack plugin registration failed\n");
    }
} install;
}
}
