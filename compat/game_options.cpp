#include "nitrogen_options.h"
#include <array>
#include <atomic>
#include <cstdlib>
#include <cstdio>
#include <fstream>
#include <mutex>
#include <sstream>
#include <string>
#if defined(__APPLE__)
#include <TargetConditionals.h>
#endif

namespace {
// Defaults keep the original presentation: AUTO aspect fills the window, VSync on.
std::array<std::atomic<int>, NITROGEN_OPTION_COUNT> values{{NITROGEN_WINDOWED, 0, 100, 100, NITROGEN_ASPECT_AUTO, 1, NITROGEN_HUD_EDGES,
                                                            NITROGEN_REFRESH_DESKTOP}};
constexpr const char *names[]{"video_mode", "hud_scale", "render_percent", "master_volume", "aspect", "vsync", "hud_layout",
                              "fullscreen_refresh"};
static_assert(sizeof(names) / sizeof(names[0]) == NITROGEN_OPTION_COUNT, "one name per option");
std::mutex preferences_mutex;
std::string preferences_path;
bool initialized = false;
void save()
{
    if (preferences_path.empty()) return;
    std::ofstream out(preferences_path, std::ios::trunc);
    for (int i = 0; i < NITROGEN_OPTION_COUNT; ++i) out << names[i] << ' ' << values[i].load() << '\n';
    if (!out) std::fprintf(stderr, "nitrogen: could not save game options\n");
}
bool valid(int key, int value)
{
    switch (key) {
    case NITROGEN_VIDEO_MODE: return value >= 0 && value <= 2;
    case NITROGEN_HUD_SCALE: return value == 0 || value == 2 || value == 3 || value == 4 || value == 6;
    case NITROGEN_RENDER_PERCENT: return value >= 25 && value <= 200 && value % 5 == 0;
    case NITROGEN_MASTER_VOLUME: return value >= 0 && value <= 100;
    case NITROGEN_ASPECT: return value >= NITROGEN_ASPECT_AUTO && value <= NITROGEN_ASPECT_32_9;
    case NITROGEN_VSYNC: return value == 0 || value == 1;
    case NITROGEN_HUD_LAYOUT: return value == NITROGEN_HUD_EDGES || value == NITROGEN_HUD_CENTRED_16_9;
    case NITROGEN_FULLSCREEN_REFRESH: return value == NITROGEN_REFRESH_DESKTOP || value == NITROGEN_REFRESH_SMOOTH_60;
    default: return false;
    }
}
}
extern "C" int nitrogen_options_desktop(void)
{
#if defined(__ANDROID__) || defined(__IPHONEOS__) || defined(__vita__) || defined(__PSP2__) || (defined(TARGET_OS_IPHONE) && TARGET_OS_IPHONE)
    return 0;
#else
    return 1;
#endif
}
extern "C" void nitrogen_options_init(const char *base)
{
    std::lock_guard<std::mutex> lock(preferences_mutex);
    if (initialized) return;
    initialized = true;
    const char *configured = std::getenv("NITROGEN_OPTIONS_FILE");
    preferences_path = configured && *configured ? configured : std::string(base ? base : "") + "nitrogen-options.ini";
    std::ifstream in(preferences_path);
    bool migrated=false;
    if (!in && base) {
        // Preserve the previous F1 master volume on the first updated launch.
        std::ifstream old(std::string(base) + "nitrogen-menu.ini");
        std::string name; int value;
        while (old >> name >> value)
            if (name == "volume" && valid(NITROGEN_MASTER_VOLUME, value)) { values[3] = value; migrated=true; }
    }
    std::string line;
    while (std::getline(in, line)) {
        std::istringstream row(line);
        std::string name, trailing; int value;
        if (!(row >> name >> value) || row >> trailing) continue;
        for (int key = 0; key < NITROGEN_OPTION_COUNT; ++key)
            if (name == names[key] && valid(key, value)) values[key] = value;
    }
    if (!nitrogen_options_desktop()) values[0] = NITROGEN_BORDERLESS;
    if (migrated) save();
}
extern "C" int nitrogen_option_get(int key)
{
    return key >= 0 && key < NITROGEN_OPTION_COUNT ? values[key].load(std::memory_order_acquire) : 0;
}
extern "C" int nitrogen_option_set(int key, int value)
{
    if (!valid(key, value) || (key == NITROGEN_VIDEO_MODE && !nitrogen_options_desktop())) return 0;
    std::lock_guard<std::mutex> lock(preferences_mutex);
    if (values[key].load() == value) return 1;
    values[key].store(value, std::memory_order_release);
    save();
    return 1;
}
