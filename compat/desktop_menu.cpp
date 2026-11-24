/* Original desktop frontend. Game media and guest memory never enter the UI. */
#include "desktop_menu.h"
#include "psx_menu_host.h"
#include "host_osd.h"
#include "runtime_telemetry.h"
#ifdef NITROGEN_WINDOW_DISPLAY
#include "window_display.h"
#include "nitrogen_options.h"
#include "gpu_gl_renderer.h"
#include "gl_scale_limits.h"
extern "C" int psx_present_vsync_owns_cadence(void);
#endif
#include "../native/movie_player.h"
#include <SDL3/SDL.h>
#include "imgui.h"
#include "imgui_impl_sdl3.h"
#include "imgui_impl_opengl3.h"
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <mutex>
#include <string>

namespace {
struct DesktopMenu {
    std::mutex mutex;
    const PsxMenuHost *host = nullptr;
    ImGuiContext *imgui = nullptr;
    SDL_Window *window = nullptr;
    std::atomic<bool> open{false};
    bool visible = true, autohide = false, settings = false, about = false;
    bool peek = false;   // F1 revealed an autohidden bar
    bool performance = false, audio = false, reset = false;
    bool escape = false, close_requested = false;
    float bar_height = 24.0f, performance_height = 0;
    std::string preferences;
    double rate_time = 0, sample_rate = 0;
    uint64_t last_samples = 0;
};
DesktopMenu menu;
std::atomic<int> reserved{0};

void save_preferences(DesktopMenu &m)
{
    if (m.preferences.empty()) return;
    std::ofstream out(m.preferences, std::ios::trunc);
    if (!out) { std::fprintf(stderr, "nitrogen: could not save menu preferences\n"); return; }
    out << "visible " << m.visible << "\nautohide " << m.autohide
        << "\nperformance " << m.performance
        << "\naudio " << m.audio << '\n';
}

#ifdef NITROGEN_WINDOW_DISPLAY
const char *world_reason_text(int reason)
{
    switch (reason) {
    case NITROGEN_WORLD_TEXTURE_LIMIT: return "limited by the graphics card's largest texture";
    case NITROGEN_WORLD_ALLOCATION_LIMIT: return "limited: the graphics card refused a larger allocation";
    case NITROGEN_WORLD_NOT_USED: return "not used on this screen (movies and 2D images keep their own size)";
    default: return "full size";
    }
}
// The in-game Video menu's choices, sharing the same saved options.
void video_settings()
{
    if (nitrogen_options_desktop()) {
        static const char *modes[] = {"Windowed", "Borderless fullscreen", "Fullscreen"};
        int mode = nitrogen_option_get(NITROGEN_VIDEO_MODE);
        if (ImGui::Combo("Window mode", &mode, modes, IM_ARRAYSIZE(modes)))
            nitrogen_option_set(NITROGEN_VIDEO_MODE, mode);
        static const char *refreshes[] = {"Desktop", "Smoothest for 60 fps"};
        int refresh = nitrogen_option_get(NITROGEN_FULLSCREEN_REFRESH);
        if (ImGui::Combo("Fullscreen refresh", &refresh, refreshes, IM_ARRAYSIZE(refreshes)))
            nitrogen_option_set(NITROGEN_FULLSCREEN_REFRESH, refresh);
        int choice = 0, override = 0; float chosen = 0;
        nitrogen_window_refresh_report(&choice, &chosen, &override);
        if (override) ImGui::TextDisabled("Set by PSX_EXCLUSIVE_REFRESH_HZ.");
        else if (choice == NITROGEN_REFRESH_SMOOTH_60)
            chosen > 0 ? ImGui::TextDisabled("Uses %.2f Hz in Fullscreen mode.", chosen)
                       : ImGui::TextDisabled("No even rate at this resolution; uses the desktop rate.");
    }
    static const int steps[] = {0, 2, 3, 4, 6};
    static const char *scales[] = {"Auto", "2x", "3x", "4x", "6x"};
    int scale = 0;
    while (scale < 4 && steps[scale] != nitrogen_option_get(NITROGEN_HUD_SCALE)) ++scale;
    if (ImGui::Combo("HUD scale", &scale, scales, IM_ARRAYSIZE(scales)))
        nitrogen_option_set(NITROGEN_HUD_SCALE, steps[scale]);
    // 25-200% in the game menu's 5% steps.
    int percent = nitrogen_option_get(NITROGEN_RENDER_PERCENT);
    if (ImGui::SliderInt("Render resolution", &percent, 25, 200, "%d%%", ImGuiSliderFlags_AlwaysClamp))
        nitrogen_option_set(NITROGEN_RENDER_PERCENT, std::clamp((percent + 2) / 5 * 5, 25, 200));
}
// The game's own Extras menu choices. The game owns these values: F1 queues
// a change that the game thread applies between frames, and only the game's
// own memory-card set-up save keeps it. Unavailable values are never guessed.
void extras_settings()
{
    static const char *flare[] = {"Diamond cut", "Damp", "Sharp"};
    static const char *lens[] = {"Normal", "Narrow", "Wide"};
    static const char *roll[] = {"Follow player", "Locked"};
    static const char *hatching[] = {"Preset", "Random"};
    static const char *bonus[] = {"Fixed", "Selectable"};
    struct Row { const char *label, **names; int extra; };
    static const Row rows[] = {
        {"Lens flare", flare, NITROGEN_EXTRA_LENS_FLARE},
        {"Lens type", lens, NITROGEN_EXTRA_LENS_TYPE},
        {"Camera roll", roll, NITROGEN_EXTRA_CAMERA_ROLL},
        {"Hatching order", hatching, NITROGEN_EXTRA_HATCHING},
        {"Bonus weapons", bonus, NITROGEN_EXTRA_BONUS_WEAPONS},
    };
    int values[NITROGEN_EXTRA_COUNT] = {};
    const bool available = nitrogen_extras_snapshot(values) != 0;
    if (!available) ImGui::TextDisabled("Not available right now.");
    ImGui::BeginDisabled(!available);
    for (const Row &row : rows) {
        int value = values[row.extra];
        if (ImGui::Combo(row.label, &value, row.names, nitrogen_extras_choices(row.extra)))
            nitrogen_extras_request(row.extra, value);
    }
    ImGui::EndDisabled();
    ImGui::TextWrapped("Some changes may take effect from the next level, as in the game's own menu. "
                       "Save your set-up in the game to keep them.");
}
// Display settings and what the renderer actually produced. Values that
// cannot be read are shown as unavailable, never guessed.
void display_settings(DesktopMenu &m)
{
    static const char *aspects[] = {"Auto (fill the window)", "Original 4:3", "16:9", "21:9", "32:9"};
    int aspect = nitrogen_option_get(NITROGEN_ASPECT);
    if (ImGui::Combo("Aspect", &aspect, aspects, IM_ARRAYSIZE(aspects)))
        nitrogen_option_set(NITROGEN_ASPECT, aspect);
    static const char *hud_layouts[] = {"Edges of the game area", "Centred (16:9)"};
    int hud = nitrogen_option_get(NITROGEN_HUD_LAYOUT);
    if (ImGui::Combo("HUD position", &hud, hud_layouts, IM_ARRAYSIZE(hud_layouts)))
        nitrogen_option_set(NITROGEN_HUD_LAYOUT, hud);
    bool vsync = nitrogen_option_get(NITROGEN_VSYNC) != 0;
    if (ImGui::Checkbox("VSync", &vsync)) nitrogen_option_set(NITROGEN_VSYNC, vsync ? 1 : 0);

    int ww = 0, wh = 0, aw = 0, ah = 0, effective = 0, env = 0;
    nitrogen_window_report(&ww, &wh, &aw, &ah, &effective, &env);
    ImGui::Text("Window: %d x %d   Game area: %d x %d", ww, wh, aw, ah);
    ImGui::Text("Interface scale: %dx", aw > 0 && ah > 0 ? nitrogen_window_ui_step(aw, ah) : 0);

    NitrogenWorldStatus world{};
    nitrogen_window_world_status(&world);
    if (world.reason == NITROGEN_WORLD_NOT_USED) {
        ImGui::Text("3D scene: %s", world_reason_text(world.reason));
    } else {
        ImGui::Text("3D scene: requested %d%% = %d x %d", world.percent, world.requested_w, world.requested_h);
        ImGui::Text("          actual %d x %d (%s)", world.actual_w, world.actual_h, world_reason_text(world.reason));
        if (world.max_texture > 0) ImGui::Text("          graphics card texture limit: %d", world.max_texture);
    }

    // The renderer's own surface report waits for the render thread: read it
    // at most once a second, and only while this window is open.
    static GlScaleInfo scale{};
    static int scale_ok = 0;
    static double scale_time = -10;
    const double now = SDL_GetTicks() / 1000.0;
    if (now - scale_time >= 1.0) { scale_ok = gl_renderer_scale_info(&scale); scale_time = now; }
    if (scale_ok && scale.fbo_w > 0) {
        ImGui::Text("Picture surface: %d x %d (scale %d)", scale.fbo_w, scale.fbo_h, scale.effective);
        if (scale.clamp_reason)
            ImGui::Text("          limited by:%s%s%s", (scale.clamp_reason & PSX_GL_SCALE_TEXTURE) ? " texture size" : "",
                        (scale.clamp_reason & PSX_GL_SCALE_BUDGET) ? " memory budget" : "",
                        (scale.clamp_reason & PSX_GL_SCALE_CEILING) ? " build ceiling" : "");
    } else {
        ImGui::TextUnformatted("Picture surface: unavailable");
    }

    const SDL_DisplayMode *mode = m.window ? SDL_GetCurrentDisplayMode(SDL_GetDisplayForWindow(m.window)) : nullptr;
    const float refresh = mode ? mode->refresh_rate : 0.0f;
    ImGui::Text("VSync in effect: %s%s", effective ? "on" : "off",
                env ? " (set by PSX_VSYNC)" : (vsync && !effective ? " (turned off by the game's configuration)" : ""));
    if (refresh > 0) {
        ImGui::Text("Display refresh: %.2f Hz; the game runs at 59.94 Hz.", refresh);
        int even = 0;
        const double per_frame = nitrogen_window_refreshes_per_frame(refresh, 60000.0 / 1001.0, &even);
        if (even)
            ImGui::Text("Refreshes per game frame: %.2f (even: smooth motion)", per_frame);
        else {
            ImGui::Text("Refreshes per game frame: %.2f (uneven)", per_frame);
            ImGui::TextWrapped("Each frame is shown for %d or %d refreshes, which can look like slight judder. "
                               "For even motion use Fullscreen with \"Smoothest for 60 fps\", or a variable "
                               "refresh display (G-SYNC / FreeSync).",
                               (int)std::floor(per_frame), (int)std::floor(per_frame) + 1);
        }
    } else {
        ImGui::TextUnformatted("Display refresh: unavailable");
    }
    ImGui::TextUnformatted("Sharp textures and video. Movies retain their proportions.");
}
#endif

void fullscreen(DesktopMenu &m)
{
#ifdef NITROGEN_WINDOW_DISPLAY
    nitrogen_option_set(NITROGEN_VIDEO_MODE, nitrogen_option_get(NITROGEN_VIDEO_MODE) == NITROGEN_WINDOWED ? NITROGEN_BORDERLESS : NITROGEN_WINDOWED);
#else
    PsxHostVideoSettings v{};
    m.host->video_get(&v);
    v.fullscreen = v.fullscreen ? 0 : 1;
    m.host->video_apply(&v);
#endif
}

int attach(void *context, void *window, void *gl, const PsxMenuHost *host)
{
    auto &m = *static_cast<DesktopMenu *>(context);
    std::lock_guard<std::mutex> lock(m.mutex);
    if (!host || host->abi_version != PSX_MENU_ABI_VERSION) return 0;
    m.host = host;
    m.window = static_cast<SDL_Window *>(window);
    m.settings = m.about = m.reset = m.escape = m.close_requested = false;
    m.open = false;
    const char *base = SDL_GetBasePath();
    m.preferences = base ? std::string(base) + "nitrogen-menu.ini" : "";
#ifdef NITROGEN_WINDOW_DISPLAY
    nitrogen_options_init(base);
    host_volume_set(nitrogen_option_get(NITROGEN_MASTER_VOLUME));
#endif
    std::ifstream in(m.preferences);
    std::string name;
    int value;
    while (in >> name >> value) {
        if (name == "visible") m.visible = value != 0;
        else if (name == "autohide") m.autohide = value != 0;
        else if (name == "performance") m.performance = value != 0;
        else if (name == "audio") m.audio = value != 0;
    }
    IMGUI_CHECKVERSION();
    m.imgui = ImGui::CreateContext();
    ImGui::SetCurrentContext(m.imgui);
    ImGuiIO &io = ImGui::GetIO();
    io.IniFilename = nullptr;
    io.LogFilename = nullptr;
    io.ConfigFlags |= ImGuiConfigFlags_NavEnableKeyboard;
    const float scale = std::clamp(SDL_GetWindowDisplayScale(m.window), 1.0f, 3.0f);
    ImFontConfig font;
    font.SizePixels = 15.0f * scale;
    std::string font_path;
#ifdef _WIN32
    if (const char *windows = std::getenv("WINDIR")) font_path = std::string(windows) + "/Fonts/segoeui.ttf";
#else
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf";
#endif
    // Use an installed host font, never copy it into the project/package.
    ImFont *loaded = nullptr;
    if (!font_path.empty() && std::ifstream(font_path, std::ios::binary).good())
        loaded = io.Fonts->AddFontFromFileTTF(font_path.c_str(), font.SizePixels);
    if (!loaded) io.Fonts->AddFontDefault(&font);
    ImGui::StyleColorsDark();
    ImGuiStyle &style = ImGui::GetStyle();
    style.WindowRounding = 5;
    style.PopupRounding = 5;
    style.FrameRounding = 3;
    style.Colors[ImGuiCol_MenuBarBg] = ImVec4(.15f, .15f, .15f, 1);
    style.Colors[ImGuiCol_PopupBg] = ImVec4(.15f, .15f, .15f, 1);
    style.ScaleAllSizes(scale);
    if (!ImGui_ImplSDL3_InitForOpenGL(m.window, gl)) {
        ImGui::DestroyContext(m.imgui); m.imgui = nullptr; return 0;
    }
    if (!ImGui_ImplOpenGL3_Init("#version 330")) {
        ImGui_ImplSDL3_Shutdown();
        ImGui::DestroyContext(m.imgui); m.imgui = nullptr; return 0;
    }
    m.bar_height = font.SizePixels + style.FramePadding.y * 2;
    reserved = m.visible && !m.autohide ? (int)std::ceil(m.bar_height) : 0;
    m.rate_time = 0; m.last_samples = 0; m.sample_rate = 0;
    return 1;
}

int event(void *context, const void *input)
{
    auto &m = *static_cast<DesktopMenu *>(context);
    std::lock_guard<std::mutex> lock(m.mutex);
    if (!m.imgui) return 0;
    ImGui::SetCurrentContext(m.imgui);
    const auto &ev = *static_cast<const SDL_Event *>(input);
    ImGui_ImplSDL3_ProcessEvent(&ev);
    if (ev.type == SDL_EVENT_KEY_DOWN && !ev.key.repeat) {
        if (ev.key.key == SDLK_F1) {
            NitrogenMenuBarState s{m.visible, m.autohide, m.peek};
            float x = 0, y = 0;
            SDL_GetMouseState(&x, &y);
            const bool changed = nitrogen_menu_bar_f1(s, m.open.load(), y >= 0 && y <= m.bar_height);
            m.visible = s.visible; m.peek = s.peek;
            if (!nitrogen_menu_bar_shown(s, false, false)) m.close_requested = true;
            if (changed) save_preferences(m);
            return 1;
        }
        if (ev.key.key == SDLK_F11) { fullscreen(m); return 1; }
        if (ev.key.key == SDLK_ESCAPE && m.open.load()) { m.escape = true; return 1; }
    }
    if (ev.type == SDL_EVENT_WINDOW_FOCUS_LOST) {
        m.close_requested = true;
        m.peek = false;
#ifdef NITROGEN_WINDOW_DISPLAY
        (void)nitrogen_window_close_hold(0,0);
#endif
    }
    const bool key = ev.type == SDL_EVENT_KEY_DOWN || ev.type == SDL_EVENT_KEY_UP ||
                     ev.type == SDL_EVENT_TEXT_INPUT || ev.type == SDL_EVENT_TEXT_EDITING;
    const bool mouse = ev.type == SDL_EVENT_MOUSE_MOTION || ev.type == SDL_EVENT_MOUSE_BUTTON_DOWN ||
                       ev.type == SDL_EVENT_MOUSE_BUTTON_UP || ev.type == SDL_EVENT_MOUSE_WHEEL;
    const bool pad = ev.type == SDL_EVENT_GAMEPAD_BUTTON_DOWN || ev.type == SDL_EVENT_GAMEPAD_BUTTON_UP ||
                     ev.type == SDL_EVENT_GAMEPAD_AXIS_MOTION;
    if (m.open && (key || mouse || pad)) return 1;
    if (mouse && m.visible) {
        float x = 0, y = 0;
        SDL_GetMouseState(&x, &y);
        if (ev.type == SDL_EVENT_MOUSE_BUTTON_DOWN || ev.type == SDL_EVENT_MOUSE_BUTTON_UP)
            y = ev.button.y;
        if (y >= 0 && y < m.bar_height) return 1;
    }
    return 0; // Never consume quit, resize, focus or device-removal events.
}

void passive_window(DesktopMenu &m, const char *title, bool audio)
{
    ImGui::SetNextWindowBgAlpha(.9f);
    ImGui::SetNextWindowPos(ImVec2(12, m.bar_height + 12 + (audio && m.performance ? m.performance_height + 8 : 0)), ImGuiCond_Always);
    ImGui::Begin(title, nullptr, ImGuiWindowFlags_AlwaysAutoResize | ImGuiWindowFlags_NoInputs |
                 ImGuiWindowFlags_NoNav | ImGuiWindowFlags_NoSavedSettings);
    if (audio) {
        NitrogenRuntimeStatus stats{};
        nitrogen_runtime_status(&stats);
        if (nitrogen_media_is_active()) {
            double seconds; uint64_t samples; nitrogen_media_get_stats(&seconds, &samples);
            ImGui::Text("Native media: %.2f seconds", seconds);
            ImGui::Text("Audio submitted: %llu samples", (unsigned long long)samples);
            ImGui::TextUnformatted("Independent audio; video follows media clock.");
        } else {
        ImGui::Text("SPU production: %.0f samples/s", m.sample_rate);
        if (stats.audio_active) {
            ImGui::Text("Output: %d Hz (%s)", stats.host_rate, stats.legacy ? "queue" : "clock bridge");
            ImGui::Text("Buffer: %.1f ms | target %.1f ms", stats.fill_ms, stats.target_ms);
            ImGui::Text("%s: %llu | overflow frames: %llu",
                stats.legacy ? "Empty queue events" : "Underflow samples",
                (unsigned long long)stats.underruns, (unsigned long long)stats.overflow_drops);
        } else ImGui::TextUnformatted("Audio output initializing.");
        ImGui::TextUnformatted("Independent output callback; guest-timed SPU/CD.");
        }
    } else {
        ImGui::Text("Display: %.1f FPS", ImGui::GetIO().Framerate);
        NitrogenRuntimeStatus stats{}; nitrogen_runtime_status(&stats);
        if (!nitrogen_media_is_active()) {
            ImGui::Text("Render worker: %s", stats.worker_active ? (stats.worker_held ? "synchronized" : "active") : "off");
            if (stats.worker_active) ImGui::Text("Queued frame bound: %d", stats.max_frames);
        }
        ImGui::TextUnformatted("Game speed: use the title-bar readout (--show-fps).");
    }
    if (!audio) m.performance_height = ImGui::GetWindowHeight();
    ImGui::End();
}

void draw(void *context, int, int)
{
    auto &m = *static_cast<DesktopMenu *>(context);
    std::lock_guard<std::mutex> lock(m.mutex);
    if (!m.imgui) return;
    ImGui::SetCurrentContext(m.imgui);
    ImGui_ImplOpenGL3_NewFrame();
    ImGui_ImplSDL3_NewFrame();
    ImGui::NewFrame();
    const bool close = m.escape || m.close_requested;
    if (close) {
        m.settings = m.about = m.reset = false;
        m.escape = m.close_requested = false;
    }
    bool changed = false, popup = false;
    const ImVec2 mouse = ImGui::GetIO().MousePos;
    const bool over_bar = mouse.x >= 0 && mouse.x < ImGui::GetIO().DisplaySize.x &&
                          mouse.y >= 0 && mouse.y <= m.bar_height;
    const bool bar = nitrogen_menu_bar_shown({m.visible, m.autohide, m.peek}, m.open, over_bar);
    if (bar && ImGui::BeginMainMenuBar()) {
        m.bar_height = ImGui::GetWindowHeight();
        if (!close && ImGui::BeginMenu("System")) {
            popup = true;
            if (ImGui::MenuItem("Settings")) m.settings = true;
            ImGui::Separator();
            changed |= ImGui::MenuItem("Show Menu Bar", "F1", &m.visible);
            if (ImGui::MenuItem("Autohide Menu Bar", nullptr, &m.autohide)) { changed = true; m.peek = false; }
            PsxHostVideoSettings v{};
            m.host->video_get(&v);
            if (ImGui::MenuItem("Fullscreen", "F11", v.fullscreen != 0)) fullscreen(m);
            ImGui::Separator();
            if (ImGui::MenuItem("Hard Reset")) m.reset = true;
            ImGui::Separator();
            if (ImGui::MenuItem("Quit Application")) m.host->request_exit(PSX_MENU_EXIT_QUIT);
            ImGui::EndMenu();
        }
        if (!close && ImGui::BeginMenu("Debug")) {
            popup = true;
            changed |= ImGui::MenuItem("Performance Overlay", nullptr, &m.performance);
            changed |= ImGui::MenuItem("Audio Status", nullptr, &m.audio);
            ImGui::Separator();
            if (ImGui::MenuItem("About This Build")) m.about = true;
            ImGui::EndMenu();
        }
        ImGui::EndMainMenuBar();
    }
    if (m.settings) {
        ImGui::SetNextWindowSize(ImVec2(390, 0), ImGuiCond_FirstUseEver);
        if (ImGui::Begin("Settings", &m.settings, ImGuiWindowFlags_AlwaysAutoResize)) {
#ifdef NITROGEN_WINDOW_DISPLAY
            ImGui::SeparatorText("Display");
            video_settings();
            display_settings(m);
            ImGui::SeparatorText("Game extras");
            extras_settings();
#else
            PsxHostVideoSettings v{};
            m.host->video_get(&v);
            bool full = v.fullscreen != 0;
            if (ImGui::Checkbox("Fullscreen", &full)) fullscreen(m);
#endif
            ImGui::SeparatorText("Menu");
            changed |= ImGui::Checkbox("Show menu bar (F1)", &m.visible);
            if (ImGui::Checkbox("Autohide menu bar", &m.autohide)) { changed = true; m.peek = false; }
            ImGui::TextUnformatted("Escape closes menus. F1 shows or hides the bar, also with autohide.");
        }
        ImGui::End();
    }
    if (m.about) {
        if (ImGui::Begin("About Nitrogen Squared", &m.about, ImGuiWindowFlags_AlwaysAutoResize)) {
            ImGui::Text("Nitrogen Squared %s", NITROGEN_MENU_VERSION);
            ImGui::TextUnformatted("Private development runtime - user-provided game.");
            ImGui::TextUnformatted("C11 / C++17 | SDL3 | OpenGL | Dear ImGui");
        }
        ImGui::End();
    }
    if (m.reset) ImGui::OpenPopup("Hard Reset?");
    if (ImGui::BeginPopupModal("Hard Reset?", nullptr, ImGuiWindowFlags_AlwaysAutoResize)) {
        ImGui::TextUnformatted("Restart the game? Unsaved progress will be lost.");
        if (ImGui::Button("Restart")) { m.reset = false; m.host->request_exit(PSX_MENU_EXIT_RELAUNCH); }
        ImGui::SameLine();
        if (ImGui::Button("Cancel") || close) { m.reset = false; ImGui::CloseCurrentPopup(); }
        ImGui::EndPopup();
    }
    NitrogenRuntimeStatus stats{};
    nitrogen_runtime_status(&stats);
    const double now = ImGui::GetTime();
    if (m.rate_time && now - m.rate_time >= 1.0) {
        const uint64_t count = stats.samples;
        m.sample_rate = count >= m.last_samples ? (count - m.last_samples) / (now - m.rate_time) : 0;
        m.last_samples = count; m.rate_time = now;
    } else if (!m.rate_time) { m.rate_time = now; m.last_samples = stats.samples; }
    if (m.performance) passive_window(m, "Performance", false);
    if (m.audio) passive_window(m, "Audio Status", true);
    m.open = popup || m.settings || m.about || m.reset;
    m.host->set_paused(m.open ? 1 : 0);
    // Autohide overlays briefly at the top; the persistent bar reserves space.
    reserved = m.visible && !m.autohide ? (int)std::ceil(m.bar_height * ImGui::GetIO().DisplayFramebufferScale.y) : 0;
    if (changed) save_preferences(m);
    ImGui::Render();
    ImGui_ImplOpenGL3_RenderDrawData(ImGui::GetDrawData());
}

int is_open(void *context) { return static_cast<DesktopMenu *>(context)->open ? 1 : 0; }
void set_open(void *context, int open)
{
    auto &m = *static_cast<DesktopMenu *>(context);
    std::lock_guard<std::mutex> lock(m.mutex);
    m.settings = open != 0;
    if (!open) m.close_requested = true;
    m.open = open != 0;
}
void detach(void *context)
{
    auto &m = *static_cast<DesktopMenu *>(context);
    std::lock_guard<std::mutex> lock(m.mutex);
    if (m.imgui) {
        ImGui::SetCurrentContext(m.imgui);
        ImGui_ImplOpenGL3_Shutdown();
        ImGui_ImplSDL3_Shutdown();
        ImGui::DestroyContext(m.imgui);
    }
    m.imgui = nullptr; m.host = nullptr; m.window = nullptr;
    m.open = false; reserved = 0;
}
const PsxMenuProvider provider = {PSX_MENU_ABI_VERSION, "Nitrogen desktop menu", &menu,
    attach, event, is_open, set_open, draw, detach};
struct Registration { Registration() { psx_menu_set_provider(&provider); } } registration;
}

extern "C" int nitrogen_menu_reserved_height(void) { return reserved.load(); }
extern "C" void nitrogen_menu_letterbox(int ww, int wh, int num, int den,
                                        int *x, int *y, int *w, int *h)
{
    const int usable = std::max(1, wh - std::clamp(reserved.load(), 0, std::max(0, wh - 1)));
    int dw = ww, dh = (int)((int64_t)ww * den / std::max(1, num));
    if (dh > usable) { dh = usable; dw = (int)((int64_t)usable * num / std::max(1, den)); }
    *x = (ww - dw) / 2; *y = (usable - dh) / 2;
    *w = dw; *h = dh;
}

// Game view below 4:3: keep the world at its native physical proportions and
// let the window crop the sides. Authored images/movies still use the fit helper.
extern "C" void nitrogen_menu_world_letterbox(int ww, int wh, int num, int den,
                                              int *x, int *y, int *w, int *h)
{
    if ((int64_t)num * 3 >= (int64_t)den * 4) {
        nitrogen_menu_letterbox(ww, wh, num, den, x, y, w, h);
        return;
    }
    const int usable = std::max(1, wh - std::clamp(reserved.load(), 0, std::max(0, wh - 1)));
    *w = usable * 4 / 3; *h = usable; *x = (ww - *w) / 2; *y = 0;
}
