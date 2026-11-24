// GPL-3.0-or-later. Original display measurement fixture.
//
// Linked separately from the unmodified production objects with
// -Wl,--wrap=SDL_PollEvent,--wrap=SDL_GL_SwapWindow,--wrap=SDL_RenderPresent.
// The player binary, its rendering, game timing and assets are unchanged:
// this file only observes, injects an original controller route through the
// framework's offline controller source (as its playtest replay does), applies the requested window/display mode, and writes
// private files beneath a .local directory. It contains no game data.
//
// Configuration: NITROGEN_MEASURE_CONFIG names a "key value" text file written
// by tools/measurement/measure_display.py.
#include "psx_sdl.h"
#include "gpu.h"
#include "gpu_gl_renderer.h"
#include "mod_plugins.h"
#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <mutex>
#include <sstream>
#include <string>
#include <vector>
#ifdef _WIN32
#include <windows.h>
#include <intrin.h>
#else
#include <time.h>
#endif
#ifndef APIENTRY
#define APIENTRY
#endif

extern "C" uint64_t s_frame_count;
extern "C" uint8_t *g_psx_ram;
extern "C" bool __real_SDL_PollEvent(SDL_Event *);
extern "C" bool __real_SDL_GL_SwapWindow(SDL_Window *);
extern "C" bool __real_SDL_RenderPresent(SDL_Renderer *);
extern "C" void nitrogen_window_world_size(int *, int *);
extern "C" int nitrogen_window_ui_step(int, int);
extern "C" int nitrogen_menu_reserved_height(void);
// Display V1 reporting. Weak: a build without V1 still links, and records none.
struct NitrogenWorldStatusV1 { int percent, requested_w, requested_h, actual_w, actual_h, reason, max_texture; };
extern "C" void nitrogen_window_world_status(NitrogenWorldStatusV1 *) __attribute__((weak));
extern "C" void nitrogen_window_report(int *, int *, int *, int *, int *, int *) __attribute__((weak));
// V1.2 game extras (maintenance): queue the same request F1 makes, and record
// what the game side published. Weak so older runtimes still link.
extern "C" void nitrogen_extras_request(int, int) __attribute__((weak));
extern "C" int nitrogen_extras_snapshot(int *) __attribute__((weak));
extern "C" int nitrogen_option_get(int) __attribute__((weak));

namespace {
// ---- time -------------------------------------------------------------
uint64_t now_ns() {
#ifdef _WIN32
    static LARGE_INTEGER f{};
    if (!f.QuadPart) QueryPerformanceFrequency(&f);
    LARGE_INTEGER c; QueryPerformanceCounter(&c);
    return (uint64_t)((long double)c.QuadPart * 1e9L / (long double)f.QuadPart);
#else
    timespec t{}; clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * 1000000000ull + (uint64_t)t.tv_nsec;
#endif
}
// Emulation-thread busy time. Linux: exact thread CPU ns. Windows: thread
// cycles (TSC units); the driver converts them with a TSC/QPC calibration.
uint64_t thread_busy() {
#ifdef _WIN32
    ULONG64 cycles = 0; QueryThreadCycleTime(GetCurrentThread(), &cycles); return cycles;
#else
    timespec t{}; clock_gettime(CLOCK_THREAD_CPUTIME_ID, &t);
    return (uint64_t)t.tv_sec * 1000000000ull + (uint64_t)t.tv_nsec;
#endif
}
uint64_t tsc() {
#if defined(_WIN32) && (defined(__x86_64__) || defined(_M_X64))
    return __rdtsc();
#else
    return 0;
#endif
}

// ---- configuration ----------------------------------------------------
struct Step { uint16_t buttons; uint32_t frames; };
// A route stage waits for a display state (scanout width, optional height),
// then applies its steps after a delay. Stage anchors make the schedule
// independent of host speed: native movies and loading follow real time, so
// absolute guest frame numbers differ between a fast and a slow PC.
struct Stage {
    // kind 0: scanout width a (and height b when non-zero) is displayed;
    // kind 1: the game's menu page is a and not leaving it (phase 29);
    // kind 2: immediately. A repeating stage cycles its steps while its
    // condition holds and ends when it stops holding (press until accepted).
    int kind = 0, repeat = 0;
    uint32_t a = 0, b = 0;
    uint64_t delay = 0, timeout = 0, anchor = 0;
    bool anchored = false;
    std::vector<Step> steps;
};
struct Config {
    std::string out, mode = "none";
    uint64_t stop = 0, apply = 120, stop_rel = 0;
    int probe = 0, width = 0, height = 0, display = -1, guest_capture = 0, present_capture = 0;
    double refresh = 0;
    std::vector<Stage> stages;
    std::vector<uint64_t> captures, scale_samples, captures_rel, scale_rel;
    std::vector<uint64_t> ram_dumps_rel;   // maintenance: guest RAM snapshots (private output)
    struct ExtraRequest { uint64_t rel; int extra, value; };
    std::vector<ExtraRequest> extras_rel;  // maintenance: F1 game-extras requests
} cfg;
bool load_config() {
    const char *path = std::getenv("NITROGEN_MEASURE_CONFIG");
    if (!path || !*path) return false;
    std::ifstream in(path);
    std::string line;
    while (std::getline(in, line)) {
        std::istringstream s(line);
        std::string key; s >> key;
        if (key == "out") { std::getline(s >> std::ws, cfg.out); }
        else if (key == "stop") s >> cfg.stop;
        else if (key == "probe") s >> cfg.probe;
        else if (key == "mode") s >> cfg.mode;
        else if (key == "width") s >> cfg.width;
        else if (key == "height") s >> cfg.height;
        else if (key == "refresh") s >> cfg.refresh;
        else if (key == "display") s >> cfg.display;
        else if (key == "apply") s >> cfg.apply;
        else if (key == "stage") {
            Stage st; s >> st.kind >> st.a >> st.b >> st.delay >> st.timeout >> st.repeat; cfg.stages.push_back(st);
        }
        else if (key == "step" && !cfg.stages.empty()) {
            unsigned b = 0, f = 0; s >> b >> f; cfg.stages.back().steps.push_back({(uint16_t)b, f});
        }
        else if (key == "stop_rel") s >> cfg.stop_rel;
        else if (key == "capture") { uint64_t f = 0; s >> f; cfg.captures.push_back(f); }
        else if (key == "capture_rel") { uint64_t f = 0; s >> f; cfg.captures_rel.push_back(f); }
        else if (key == "ram_dump_rel") { uint64_t f = 0; s >> f; cfg.ram_dumps_rel.push_back(f); }
        else if (key == "extra_rel") { Config::ExtraRequest r{}; s >> r.rel >> r.extra >> r.value; cfg.extras_rel.push_back(r); }
        else if (key == "scale_sample") { uint64_t f = 0; s >> f; cfg.scale_samples.push_back(f); }
        else if (key == "scale_rel") { uint64_t f = 0; s >> f; cfg.scale_rel.push_back(f); }
        else if (key == "guest_capture") s >> cfg.guest_capture;
        else if (key == "present_capture") s >> cfg.present_capture;
    }
    return !cfg.out.empty() && cfg.stop > 0;
}
bool private_output(const std::string &dir) {
    for (const auto &part : std::filesystem::path(dir)) if (part == ".local") return true;
    return false;
}

// ---- JSON helpers -----------------------------------------------------
std::string esc(const char *s) {
    std::string o = "\"";
    for (const char *p = s ? s : ""; *p; ++p) {
        const unsigned char c = (unsigned char)*p;
        if (c == '"' || c == '\\') { o += '\\'; o += (char)c; }
        else if (c < 0x20) { char b[8]; std::snprintf(b, sizeof b, "\\u%04x", c); o += b; }
        else o += (char)c;
    }
    return o + "\"";
}
std::mutex info_mutex;
std::vector<std::string> info_fields;          // "key": value pairs, written at exit
void info(const std::string &key, const std::string &json) {
    std::lock_guard<std::mutex> lock(info_mutex);
    info_fields.push_back(esc(key.c_str()) + ":" + json);
}
std::string mode_json(const SDL_DisplayMode *m) {
    if (!m) return "null";
    char b[160];
    std::snprintf(b, sizeof b, "{\"w\":%d,\"h\":%d,\"refresh\":%.3f,\"density\":%.3f,\"format\":%u}",
                  m->w, m->h, (double)m->refresh_rate, (double)m->pixel_density, (unsigned)m->format);
    return b;
}

std::string current_mode_json(SDL_DisplayID d) {
    // psx_sdl.h maps SDL_GetCurrentDisplayMode to its (display, out) compatibility form.
    SDL_DisplayMode m{};
    if (!d || SDL_GetCurrentDisplayMode(d, &m) != 0) return "null";
    return mode_json(&m);
}

// ---- per-frame and per-present records -------------------------------
struct FrameRow {
    uint64_t frame, t, busy, rth_frames, rth_cpu, rth_gpu, rth_gpu_frames;
    uint32_t dx, dy, w, h; int depth24, disabled, instrumented;
    uint32_t page, next_page, old_page, buttons;
};
struct SwapRow { uint64_t before, after, frame; int ok, instrumented; };
std::vector<FrameRow> frames;
constexpr size_t SWAP_CAP = 1u << 20;
std::vector<SwapRow> swaps(SWAP_CAP);
std::atomic<size_t> swap_count{0};
std::atomic<int> present_kind{0};              // 1 = OpenGL swap, 2 = SDL renderer present

bool started = false;
// The game's menu page words (also read by native/options_entry.cpp). Read
// only; phase 29/30 are transitions that resolve to the old/next page.
constexpr uint32_t MENU_PAGE = 0x90624;
uint32_t ram_word(uint32_t physical) {
    if (!g_psx_ram || physical > 0x200000 - 4) return 0;
    uint32_t v; std::memcpy(&v, g_psx_ram + physical, 4); return v;
}
uint32_t effective_page() {
    const uint32_t phase = ram_word(MENU_PAGE);
    return phase == 29 ? ram_word(MENU_PAGE + 8) : phase == 30 ? ram_word(MENU_PAGE + 4) : phase;
}
uint64_t last_frame = ~0ull, t0 = 0, tsc0 = 0;
bool quit_sent = false, applied = false, written = false;
std::atomic<uint64_t> present_target{~0ull};
std::atomic<int> present_pending{0};
std::atomic<uint64_t> present_name{0};
uint64_t source_calls = 0, source_registrations = 0, source_last_frame = 0;
size_t stage_index = 0;
uint64_t stage_wait_since = 0, measure_anchor = 0;   // 0 = the last stage is not anchored yet
uint16_t route_now = 0xFFFF;                         // buttons for the current guest frame
bool route_failed = false;


// Advance the stage machine once per guest frame (main thread, from the poll
// wrapper). The controller source reads the result for that frame.
bool stage_condition(const Stage &st, const GpuDisplayInfo &di) {
    if (st.kind == 0) return di.width == st.a && (!st.b || di.height == st.b) && !di.disabled;
    if (st.kind == 1) return effective_page() == st.a && ram_word(MENU_PAGE) != 29;
    return true;
}
void route_update(uint64_t frame, const GpuDisplayInfo &di) {
    route_now = 0xFFFF;
    while (stage_index < cfg.stages.size()) {
        Stage &st = cfg.stages[stage_index];
        const bool holds = stage_condition(st, di);
        if (!st.anchored) {
            if (holds) {
                st.anchored = true; st.anchor = frame;
                char b[200];
                std::snprintf(b, sizeof b, "{\"stage\":%zu,\"frame\":%llu,\"width\":%u,\"height\":%u,\"page\":%u}",
                              stage_index, (unsigned long long)frame, di.width, di.height, ram_word(MENU_PAGE));
                info("stage_anchor", b);
                if (stage_index + 1 == cfg.stages.size()) measure_anchor = frame;
            } else {
                if (st.timeout && frame > stage_wait_since + st.timeout && !route_failed) {
                    route_failed = true;
                    char b[160];
                    std::snprintf(b, sizeof b, "{\"stage\":%zu,\"frame\":%llu,\"page\":%u,\"width\":%u}",
                                  stage_index, (unsigned long long)frame, ram_word(MENU_PAGE), di.width);
                    info("route_failure", b);
                }
                return;
            }
        }
        uint64_t t = frame - st.anchor;
        if (t < st.delay) return;
        t -= st.delay;
        if (st.repeat) {
            uint64_t cycle = 0;
            for (const Step &s : st.steps) cycle += s.frames;
            if (holds && cycle) {
                if (st.timeout && t > st.timeout && !route_failed) {
                    route_failed = true;
                    char b[120];
                    std::snprintf(b, sizeof b, "{\"stage\":%zu,\"frame\":%llu,\"repeat\":true}",
                                  stage_index, (unsigned long long)frame);
                    info("route_failure", b);
                }
                uint64_t at = 0, phase = t % cycle;
                for (const Step &s : st.steps) {
                    if (phase < at + s.frames) { route_now = s.buttons; return; }
                    at += s.frames;
                }
                return;
            }
        } else {
            uint64_t at = 0;
            for (const Step &s : st.steps) {
                if (t < at + s.frames) { route_now = s.buttons; return; }
                at += s.frames;
            }
        }
        char b[120];
        std::snprintf(b, sizeof b, "{\"stage\":%zu,\"frame\":%llu}", stage_index, (unsigned long long)frame);
        info("stage_done", b);
        ++stage_index;                 // finished: wait for the next stage's condition
        stage_wait_since = frame;
    }
}
uint64_t relative(uint64_t frame) { return measure_anchor ? frame - measure_anchor : ~0ull; }

// The framework's offline controller source (mod_plugins.h): called once per
// guest frame in the normal pad sampler on the main thread, the same path its
// own playtest replay uses. Buttons are active-low and combine with physical
// input, so the route is applied on exact guest frames in every run.
int route_source(PSXModControllerState *state) {
    if (!state) return 0;
    ++source_calls;
    source_last_frame = s_frame_count;
    state->buttons = route_now;
    state->lx = state->ly = state->rx = state->ry = 0x80;
    state->analog = 1;
    return 1;
}
void ensure_source(uint64_t frame) {
    // A mod/session reset detaches sources; re-register if calls stop.
    if (!source_registrations || (frame > source_last_frame + 2 && frame > 2)) {
        if (psx_mod_set_controller_source(0, route_source)) ++source_registrations;
        source_last_frame = frame;
    }
}

SDL_Window *game_window() {
    int count = 0;
    SDL_Window **list = SDL_GetWindows(&count);
    SDL_Window *w = (list && count > 0) ? list[0] : nullptr;
    SDL_free(list);
    return w;
}

void record_displays() {
    int count = 0;
    SDL_DisplayID *ids = SDL_GetDisplays(&count);
    std::string out = "[";
    for (int i = 0; ids && i < count; ++i) {
        SDL_Rect r{}; SDL_GetDisplayBounds(ids[i], &r);
        int nmodes = 0;
        SDL_DisplayMode **modes = SDL_GetFullscreenDisplayModes(ids[i], &nmodes);
        std::string list = "[";
        for (int m = 0; modes && m < nmodes; ++m) list += (m ? "," : "") + mode_json(modes[m]);
        list += "]";
        SDL_free(modes);
        char head[200];
        std::snprintf(head, sizeof head, "{\"index\":%d,\"id\":%u,\"bounds\":[%d,%d,%d,%d],\"content_scale\":%.3f,",
                      i, (unsigned)ids[i], r.x, r.y, r.w, r.h, (double)SDL_GetDisplayContentScale(ids[i]));
        out += std::string(i ? "," : "") + head + "\"name\":" + esc(SDL_GetDisplayName(ids[i])) +
               ",\"desktop\":" + mode_json(SDL_GetDesktopDisplayMode(ids[i])) +
               ",\"current\":" + current_mode_json(ids[i]) + ",\"modes\":" + list + "}";
    }
    SDL_free(ids);
    info("displays", out + "]");
    info("video_driver", esc(SDL_GetCurrentVideoDriver()));
}

SDL_DisplayID target_display(SDL_Window *w) {
    int count = 0;
    SDL_DisplayID *ids = SDL_GetDisplays(&count);
    SDL_DisplayID chosen = w ? SDL_GetDisplayForWindow(w) : 0;
    if (ids && cfg.display >= 0 && cfg.display < count) chosen = ids[cfg.display];
    SDL_free(ids);
    return chosen;
}

void window_state(const char *key) {
    SDL_Window *w = game_window();
    if (!w) { info(key, "null"); return; }
    int pw = 0, ph = 0, ww = 0, wh = 0, x = 0, y = 0;
    SDL_GetWindowSizeInPixels(w, &pw, &ph); SDL_GetWindowSize(w, &ww, &wh); SDL_GetWindowPosition(w, &x, &y);
    const SDL_DisplayID d = SDL_GetDisplayForWindow(w);
    char b[400];
    std::snprintf(b, sizeof b, "{\"frame\":%llu,\"pixels\":[%d,%d],\"size\":[%d,%d],\"position\":[%d,%d],"
                  "\"fullscreen\":%d,\"display_id\":%u,\"display_scale\":%.3f,\"menu_reserved\":%d,"
                  "\"ui_step\":%d,",
                  (unsigned long long)s_frame_count, pw, ph, ww, wh, x, y,
                  (SDL_GetWindowFlags(w) & SDL_WINDOW_FULLSCREEN) ? 1 : 0, (unsigned)d,
                  (double)SDL_GetWindowDisplayScale(w), nitrogen_menu_reserved_height(),
                  nitrogen_window_ui_step(pw, ph - nitrogen_menu_reserved_height()));
    int world_w = 0, world_h = 0; nitrogen_window_world_size(&world_w, &world_h);
    char tail[120]; std::snprintf(tail, sizeof tail, "\"world_raster\":[%d,%d],", world_w, world_h);
    info(key, std::string(b) + tail + "\"window_mode\":" + mode_json(SDL_GetWindowFullscreenMode(w)) +
                  ",\"display_mode\":" + current_mode_json(d) + "}");
}

void apply_window() {
    SDL_Window *w = game_window();
    if (!w) { info("apply", "{\"ok\":false,\"reason\":\"no window\"}"); return; }
    const SDL_DisplayID d = target_display(w);
    SDL_Rect r{}; if (d) SDL_GetDisplayBounds(d, &r);
    bool ok = true; std::string reason;
    if (cfg.mode == "windowed") {
        SDL_SetWindowFullscreen(w, false);
        SDL_SetWindowPosition(w, r.x, r.y);
        ok = SDL_SetWindowSize(w, cfg.width, cfg.height);
        SDL_SyncWindow(w);
    } else if (cfg.mode == "exclusive") {
        int n = 0;
        SDL_DisplayMode **modes = d ? SDL_GetFullscreenDisplayModes(d, &n) : nullptr;
        const SDL_DisplayMode *pick = nullptr;
        for (int i = 0; modes && i < n; ++i)
            if (modes[i]->w == cfg.width && modes[i]->h == cfg.height &&
                std::fabs(modes[i]->refresh_rate - cfg.refresh) < 0.6 &&
                (!pick || std::fabs(modes[i]->pixel_density - 1.0f) < std::fabs(pick->pixel_density - 1.0f)))
                pick = modes[i];
        if (!pick) { ok = false; reason = "requested mode is not listed by this display"; }
        else {
            SDL_SetWindowPosition(w, r.x, r.y);
            ok = SDL_SetWindowFullscreenMode(w, pick) && SDL_SetWindowFullscreen(w, true);
            SDL_SyncWindow(w);
            if (!ok) reason = SDL_GetError();
        }
        SDL_free(modes);
    }
    info("apply", std::string("{\"ok\":") + (ok ? "true" : "false") + ",\"reason\":" + esc(reason.c_str()) + "}");
    if (!ok) { quit_sent = true; SDL_Event e{}; e.type = SDL_EVENT_QUIT; SDL_PushEvent(&e); }
}

void scale_sample() {
    GlScaleInfo s{};
    const int ok = gl_renderer_scale_info(&s);
    char b[700];
    std::snprintf(b, sizeof b,
        "{\"frame\":%llu,\"ok\":%d,\"requested\":%d,\"effective\":%d,\"max_scale\":%d,\"max_dim\":%d,"
        "\"clamp_reason\":%d,\"alloc_retries\":%d,\"budget_mib\":%d,\"fbo\":[%d,%d],\"drawable\":[%d,%d],"
        "\"hr_scale\":%d,\"windowed\":%d,\"window_tiles\":%d,\"window_mib\":%d,\"alloc_scale\":%d}",
        (unsigned long long)s_frame_count, ok, s.requested, s.effective, s.max_scale, s.max_dim,
        s.clamp_reason, s.alloc_retries, s.budget_mib, s.fbo_w, s.fbo_h, s.drawable_w, s.drawable_h,
        s.hr_scale, s.windowed, s.window_tiles, s.window_mib, s.alloc_scale);
    info("scale_sample", b);
    window_state("window_sample");
    if (nitrogen_window_world_status && nitrogen_window_report && nitrogen_option_get) {
        NitrogenWorldStatusV1 w{};
        nitrogen_window_world_status(&w);
        int ww = 0, wh = 0, aw = 0, ah = 0, vsync = 0, env = 0;
        nitrogen_window_report(&ww, &wh, &aw, &ah, &vsync, &env);
        char v[600];
        // Option indices: 4 = aspect, 5 = vsync, 6 = hud_layout (compat/nitrogen_options.h).
        std::snprintf(v, sizeof v,
            "{\"frame\":%llu,\"aspect_option\":%d,\"hud_layout_option\":%d,\"vsync_option\":%d,\"vsync_effective\":%d,"
            "\"vsync_env_override\":%d,\"window\":[%d,%d],\"game_area\":[%d,%d],"
            "\"world_percent\":%d,\"world_requested\":[%d,%d],\"world_actual\":[%d,%d],"
            "\"world_reason\":%d,\"max_texture\":%d}",
            (unsigned long long)s_frame_count, nitrogen_option_get(4), nitrogen_option_get(6), nitrogen_option_get(5),
            vsync, env,
            ww, wh, aw, ah, w.percent, w.requested_w, w.requested_h, w.actual_w, w.actual_h, w.reason,
            w.max_texture);
        info("display_status", v);
    }
}

void write_raw(const std::string &name, int w, int h, int channels, const uint8_t *data) {
    FILE *f = std::fopen((cfg.out + "/" + name).c_str(), "wb");
    if (!f) return;
    const uint32_t head[4] = {0x5741524Eu /* NRAW */, (uint32_t)w, (uint32_t)h, (uint32_t)channels};
    std::fwrite(head, sizeof head, 1, f);
    std::fwrite(data, (size_t)w * h * channels, 1, f);
    std::fclose(f);
}
void guest_capture(uint64_t frame) {
    static std::vector<uint16_t> vram(1024 * 512);
    GpuDisplayInfo di{}; gpu_get_display_info(&di);
    const char *source = "gl";
    if (!gl_renderer_fbo_peek(0, 0, 1024, 512, vram.data())) {
        const uint16_t *cpu = gpu_get_vram();
        if (!cpu) return;
        std::memcpy(vram.data(), cpu, vram.size() * 2);
        source = "cpu";
    }
    if (!di.width || !di.height || di.width > 1024 || di.height > 512) return;
    std::vector<uint8_t> rgb((size_t)di.width * di.height * 3);
    for (uint32_t y = 0; y < di.height; ++y)
        for (uint32_t x = 0; x < di.width; ++x) {
            uint8_t *p = &rgb[((size_t)y * di.width + x) * 3];
            gpu_display_pixel_rgb_in(vram.data(), &di, x, y, &p[0], &p[1], &p[2]);
        }
    char name[64]; std::snprintf(name, sizeof name, "guest-%llu.raw", (unsigned long long)frame);
    write_raw(name, (int)di.width, (int)di.height, 3, rgb.data());
    char b[160]; std::snprintf(b, sizeof b, "{\"frame\":%llu,\"file\":\"%s\",\"source\":\"%s\",\"w\":%u,\"h\":%u}",
                               (unsigned long long)frame, name, source, di.width, di.height);
    info("guest_capture", b);
}

// ---- OpenGL (present thread) -----------------------------------------
using GetString = const unsigned char *(APIENTRY *)(unsigned);
using GetStringi = const unsigned char *(APIENTRY *)(unsigned, unsigned);
using GetIntegerv = void (APIENTRY *)(unsigned, int *);
using ReadPixels = void (APIENTRY *)(int, int, int, int, unsigned, unsigned, void *);
using BindFramebuffer = void (APIENTRY *)(unsigned, unsigned);
using ReadBuffer = void (APIENTRY *)(unsigned);
using PixelStorei = void (APIENTRY *)(unsigned, int);
using BindBuffer = void (APIENTRY *)(unsigned, unsigned);
using GetError = unsigned (APIENTRY *)(void);
struct Gl {
    GetString get_string = nullptr; GetStringi get_stringi = nullptr; GetIntegerv get_integerv = nullptr;
    ReadPixels read_pixels = nullptr; BindFramebuffer bind_framebuffer = nullptr; ReadBuffer read_buffer = nullptr;
    PixelStorei pixel_storei = nullptr; BindBuffer bind_buffer = nullptr; GetError get_error = nullptr;
    bool nvx = false, ati = false, ready = false;
} glp;
template <class T> T proc(const char *name) { return reinterpret_cast<T>(SDL_GL_GetProcAddress(name)); }
bool has_extension(const char *name) {
    int n = 0;
    if (glp.get_stringi) {
        glp.get_integerv(0x821D /* GL_NUM_EXTENSIONS */, &n);
        while (glp.get_error && glp.get_error()) {}
        for (int i = 0; i < n; ++i) {
            const unsigned char *e = glp.get_stringi(0x1F03 /* GL_EXTENSIONS */, (unsigned)i);
            if (e && std::strcmp((const char *)e, name) == 0) return true;
        }
        if (n > 0) return false;
    }
    const unsigned char *all = glp.get_string(0x1F03);
    return all && std::strstr((const char *)all, name);
}
void gl_init() {
    glp.get_string = proc<GetString>("glGetString");
    glp.get_stringi = proc<GetStringi>("glGetStringi");
    glp.get_integerv = proc<GetIntegerv>("glGetIntegerv");
    glp.read_pixels = proc<ReadPixels>("glReadPixels");
    glp.bind_framebuffer = proc<BindFramebuffer>("glBindFramebuffer");
    glp.read_buffer = proc<ReadBuffer>("glReadBuffer");
    glp.pixel_storei = proc<PixelStorei>("glPixelStorei");
    glp.bind_buffer = proc<BindBuffer>("glBindBuffer");
    glp.get_error = proc<GetError>("glGetError");
    if (!glp.get_string || !glp.get_integerv) return;
    glp.ready = true;
    int max_tex = 0, max_rb = 0, vp[2] = {0, 0};
    glp.get_integerv(0x0D33 /* GL_MAX_TEXTURE_SIZE */, &max_tex);
    glp.get_integerv(0x84E8 /* GL_MAX_RENDERBUFFER_SIZE */, &max_rb);
    glp.get_integerv(0x0D3A /* GL_MAX_VIEWPORT_DIMS */, vp);
    glp.nvx = has_extension("GL_NVX_gpu_memory_info");
    glp.ati = has_extension("GL_ATI_meminfo");
    const bool timer = has_extension("GL_ARB_timer_query") || has_extension("GL_EXT_disjoint_timer_query");
    char b[400];
    std::snprintf(b, sizeof b, "{\"max_texture_size\":%d,\"max_renderbuffer_size\":%d,\"max_viewport\":[%d,%d],"
                  "\"timer_query_extension\":%s,\"nvx_gpu_memory_info\":%s,\"ati_meminfo\":%s,",
                  max_tex, max_rb, vp[0], vp[1], timer ? "true" : "false", glp.nvx ? "true" : "false",
                  glp.ati ? "true" : "false");
    info("gl", std::string(b) + "\"vendor\":" + esc((const char *)glp.get_string(0x1F00)) +
                   ",\"renderer\":" + esc((const char *)glp.get_string(0x1F01)) +
                   ",\"version\":" + esc((const char *)glp.get_string(0x1F02)) +
                   ",\"glsl\":" + esc((const char *)glp.get_string(0x8B8C)) + "}");
    while (glp.get_error && glp.get_error()) {}
}
void memory_sample() {
    if (!glp.ready) return;
    if (glp.nvx) {
        int dedicated = 0, total = 0, available = 0, evictions = 0, evicted = 0;
        glp.get_integerv(0x9047, &dedicated); glp.get_integerv(0x9048, &total);
        glp.get_integerv(0x9049, &available); glp.get_integerv(0x904A, &evictions);
        glp.get_integerv(0x904B, &evicted);
        char b[220];
        std::snprintf(b, sizeof b, "{\"frame\":%llu,\"source\":\"NVX\",\"dedicated_kib\":%d,\"total_kib\":%d,"
                      "\"available_kib\":%d,\"evictions\":%d,\"evicted_kib\":%d}",
                      (unsigned long long)s_frame_count, dedicated, total, available, evictions, evicted);
        info("memory_sample", b);
    } else if (glp.ati) {
        int free_tex[4] = {0, 0, 0, 0};
        glp.get_integerv(0x87FC /* GL_TEXTURE_FREE_MEMORY_ATI */, free_tex);
        char b[160];
        std::snprintf(b, sizeof b, "{\"frame\":%llu,\"source\":\"ATI\",\"texture_free_kib\":%d}",
                      (unsigned long long)s_frame_count, free_tex[0]);
        info("memory_sample", b);
    }
    while (glp.get_error && glp.get_error()) {}
}
void present_capture(SDL_Window *w, uint64_t frame) {
    if (!glp.ready || !glp.read_pixels || !glp.bind_framebuffer) return;
    int pw = 0, ph = 0; SDL_GetWindowSizeInPixels(w, &pw, &ph);
    if (pw <= 0 || ph <= 0) return;
    int read_fb = 0, read_buf = 0, pack = 0, pbo = 0;
    glp.get_integerv(0x8CAA /* GL_READ_FRAMEBUFFER_BINDING */, &read_fb);
    glp.get_integerv(0x0C02 /* GL_READ_BUFFER */, &read_buf);
    glp.get_integerv(0x0D05 /* GL_PACK_ALIGNMENT */, &pack);
    glp.get_integerv(0x88ED /* GL_PIXEL_PACK_BUFFER_BINDING */, &pbo);
    glp.bind_framebuffer(0x8CA8 /* GL_READ_FRAMEBUFFER */, 0);
    if (glp.read_buffer) glp.read_buffer(0x0405 /* GL_BACK */);
    if (glp.bind_buffer) glp.bind_buffer(0x88EB /* GL_PIXEL_PACK_BUFFER */, 0);
    if (glp.pixel_storei) glp.pixel_storei(0x0D05, 1);
    std::vector<uint8_t> rgba((size_t)pw * ph * 4);
    glp.read_pixels(0, 0, pw, ph, 0x1908 /* GL_RGBA */, 0x1401 /* GL_UNSIGNED_BYTE */, rgba.data());
    const unsigned err = glp.get_error ? glp.get_error() : 0;
    if (glp.pixel_storei) glp.pixel_storei(0x0D05, pack);
    if (glp.bind_buffer) glp.bind_buffer(0x88EB, (unsigned)pbo);
    if (glp.read_buffer) glp.read_buffer((unsigned)read_buf);
    glp.bind_framebuffer(0x8CA8, (unsigned)read_fb);
    char name[64]; std::snprintf(name, sizeof name, "present-%llu.raw", (unsigned long long)frame);
    if (!err) write_raw(name, pw, ph, 4, rgba.data());   // bottom-up rows; the driver flips them
    char b[200]; std::snprintf(b, sizeof b, "{\"frame\":%llu,\"file\":\"%s\",\"w\":%d,\"h\":%d,\"gl_error\":%u}",
                               (unsigned long long)frame, err ? "" : name, pw, ph, err);
    info("present_capture", b);
}
void record_swap(uint64_t before, uint64_t after, int ok, int instrumented) {
    const size_t i = swap_count.load(std::memory_order_relaxed);
    if (i >= SWAP_CAP) return;
    swaps[i] = {before, after, *(volatile uint64_t *)&s_frame_count, ok, instrumented};
    swap_count.store(i + 1, std::memory_order_release);
}

void write_all() {
    if (written) return;
    written = true;
    FILE *f = std::fopen((cfg.out + "/frames.csv").c_str(), "w");
    if (f) {
        std::fprintf(f, "frame,t_ns,busy,rth_frames,rth_cpu_ns,rth_gpu_ns,rth_gpu_frames,display_x,display_y,width,height,depth24,disabled,instrumented,page,next_page,old_page,buttons\n");
        for (const FrameRow &r : frames)
            std::fprintf(f, "%llu,%llu,%llu,%llu,%llu,%llu,%llu,%u,%u,%u,%u,%d,%d,%d,%u,%u,%u,%u\n",
                (unsigned long long)r.frame, (unsigned long long)r.t, (unsigned long long)r.busy,
                (unsigned long long)r.rth_frames, (unsigned long long)r.rth_cpu, (unsigned long long)r.rth_gpu,
                (unsigned long long)r.rth_gpu_frames, r.dx, r.dy, r.w, r.h, r.depth24, r.disabled, r.instrumented,
                r.page, r.next_page, r.old_page, r.buttons);
        std::fclose(f);
    }
    f = std::fopen((cfg.out + "/swaps.csv").c_str(), "w");
    if (f) {
        std::fprintf(f, "before_ns,after_ns,guest_frame,ok,instrumented\n");
        const size_t n = swap_count.load(std::memory_order_acquire);
        for (size_t i = 0; i < n; ++i)
            std::fprintf(f, "%llu,%llu,%llu,%d,%d\n", (unsigned long long)swaps[i].before,
                         (unsigned long long)swaps[i].after, (unsigned long long)swaps[i].frame,
                         swaps[i].ok, swaps[i].instrumented);
        std::fclose(f);
    }
    char b[360];
    std::snprintf(b, sizeof b, "{\"start_ns\":%llu,\"end_ns\":%llu,\"tsc_start\":%llu,\"tsc_end\":%llu,"
                  "\"busy_unit\":\"%s\",\"present_kind\":%d,\"source_calls\":%llu,\"source_registrations\":%llu,\"measure_anchor\":%llu,\"route_failed\":%d}",
                  (unsigned long long)t0, (unsigned long long)now_ns(), (unsigned long long)tsc0,
                  (unsigned long long)tsc(),
#ifdef _WIN32
                  "thread_cycles",
#else
                  "thread_cpu_ns",
#endif
                  present_kind.load(), (unsigned long long)source_calls,
                  (unsigned long long)source_registrations, (unsigned long long)measure_anchor,
                  route_failed ? 1 : 0);
    info("timing", b);
    f = std::fopen((cfg.out + "/fixture-info.json").c_str(), "w");
    if (f) {
        std::lock_guard<std::mutex> lock(info_mutex);
        std::fprintf(f, "[\n");
        for (size_t i = 0; i < info_fields.size(); ++i)
            std::fprintf(f, "{%s}%s\n", info_fields[i].c_str(), i + 1 < info_fields.size() ? "," : "");
        std::fprintf(f, "]\n");
        std::fclose(f);
    }
}
}  // namespace

extern "C" bool __wrap_SDL_PollEvent(SDL_Event *event) {
    if (!started) {
        started = true;
        if (!load_config() || !private_output(cfg.out)) {
            std::fprintf(stderr, "Display measurement requires NITROGEN_MEASURE_CONFIG with a .local output\n");
            std::exit(94);
        }
        frames.reserve((size_t)cfg.stop + 64);
        t0 = now_ns(); tsc0 = tsc();
        std::atexit(write_all);
        gl_renderer_render_thread_measure(1);
        record_displays();
    }
    const uint64_t frame = s_frame_count;
    if (frame != last_frame) {
        last_frame = frame;
        int instrumented = 0;
        GpuDisplayInfo di{}; gpu_get_display_info(&di);
        route_update(frame, di);
        ensure_source(frame);
        if (!applied && frame >= cfg.apply) {
            applied = true; instrumented = 1;
            window_state("window_before");
            if (cfg.mode != "none") apply_window();
            window_state("window_after");
        }
        const uint64_t rel = relative(frame);
        bool sample = false, capture = false;
        for (uint64_t s : cfg.scale_samples) sample |= s == frame;
        for (uint64_t s : cfg.scale_rel) sample |= s == rel;
        for (uint64_t c : cfg.captures) capture |= c == frame;
        for (uint64_t c : cfg.captures_rel) capture |= c == rel;
        for (uint64_t d : cfg.ram_dumps_rel)
            if (d == rel) {   // read-only copy of guest RAM into the private .local output
                char name[64]; std::snprintf(name, sizeof name, "/ram-%llu.bin", (unsigned long long)rel);
                if (FILE *f = std::fopen((cfg.out + name).c_str(), "wb")) {
                    std::fwrite(g_psx_ram, 1, 0x200000, f); std::fclose(f);
                }
                if (nitrogen_extras_snapshot) {
                    int v[5] = {};
                    const int available = nitrogen_extras_snapshot(v);
                    std::snprintf(name, sizeof name, "/extras-%llu.txt", (unsigned long long)rel);
                    if (FILE *f = std::fopen((cfg.out + name).c_str(), "w")) {
                        std::fprintf(f, "%d %d %d %d %d %d\n", available, v[0], v[1], v[2], v[3], v[4]);
                        std::fclose(f);
                    }
                }
                instrumented = 1;
            }
        for (const auto &r : cfg.extras_rel)
            if (r.rel == rel && nitrogen_extras_request) nitrogen_extras_request(r.extra, r.value);
        if (sample) { scale_sample(); instrumented = 1; }
        if (capture) {
            const uint64_t name = cfg.captures_rel.empty() ? frame : rel;   // relative names compare across hosts
            if (cfg.guest_capture) { guest_capture(name); instrumented = 1; }
            if (cfg.present_capture) { present_target.store(frame); present_name.store(name); present_pending.store(1); }
        }
        GlRthCosts costs{}; gl_renderer_render_thread_costs(&costs);
        frames.push_back({frame, now_ns(), thread_busy(), costs.frames, costs.cpu_ns, costs.gpu_ns,
                          costs.gpu_frames, di.display_x, di.display_y, di.width, di.height,
                          di.depth24, di.disabled, instrumented, ram_word(MENU_PAGE), ram_word(MENU_PAGE + 4),
                          ram_word(MENU_PAGE + 8), route_now});
        if (frame % 600 == 0) { std::fprintf(stdout, "MEASURE frame=%llu\n", (unsigned long long)frame); std::fflush(stdout); }
        if (cfg.probe && applied && frame >= cfg.apply + 30 && !quit_sent) {
            window_state("window_probe");
            quit_sent = true; *event = {}; event->type = SDL_EVENT_QUIT; return true;
        }
    }
    const bool done = frame >= cfg.stop || route_failed || (cfg.stop_rel && relative(frame) != ~0ull &&
                                                            relative(frame) >= cfg.stop_rel);
    if (done && !quit_sent) {
        quit_sent = true; *event = {}; event->type = SDL_EVENT_QUIT;
        std::fprintf(stdout, "MEASURE requested SDL_QUIT at frame=%llu\n", (unsigned long long)frame);
        std::fflush(stdout);
        return true;
    }
    return __real_SDL_PollEvent(event);
}

extern "C" bool __wrap_SDL_GL_SwapWindow(SDL_Window *window) {
    static bool first = true;
    static uint64_t swaps_seen = 0;
    if (first) { first = false; gl_init(); memory_sample(); }
    present_kind.store(1, std::memory_order_relaxed);
    int instrumented = 0;
    const uint64_t before = now_ns();
    if (present_pending.load(std::memory_order_acquire) && s_frame_count >= present_target.load()) {
        present_pending.store(0);
        present_capture(window, present_name.load());
        instrumented = 1;
    }
    if (++swaps_seen % 240 == 0) memory_sample();
    const bool ok = __real_SDL_GL_SwapWindow(window);
    record_swap(before, now_ns(), ok ? 1 : 0, instrumented);
    return ok;
}

extern "C" bool __wrap_SDL_RenderPresent(SDL_Renderer *renderer) {
    // The software renderer presents through SDL_Renderer; record its timing too.
    if (present_kind.load(std::memory_order_relaxed) != 1) present_kind.store(2, std::memory_order_relaxed);
    const uint64_t before = now_ns();
    const bool ok = __real_SDL_RenderPresent(renderer);
    record_swap(before, now_ns(), ok ? 1 : 0, 0);
    return ok;
}
