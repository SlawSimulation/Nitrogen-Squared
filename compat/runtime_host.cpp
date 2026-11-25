/* Original host bridge; the pinned runtime main remains the implementation. */
#include "runtime_telemetry.h"
#include "window_title.h"
#include "psx_sdl.h"
#include "host_hotkeys.h"
#ifdef NITROGEN_WINDOW_DISPLAY
#include "window_display.h"
#include "nitrogen_options.h"
static void nitrogen_window_update();
#endif
static const char *nitrogen_base_window_title(SDL_Window *window)
{
    static NitrogenWindowTitle title;
    return title.get(SDL_GetWindowID(window), SDL_GetWindowTitle(window));
}
// The pinned main reads the title only when it re-anchors FPS telemetry.
// Pausing clears that FPS baseline; the window's original title stays owned
// by its window ID, rather than being re-read from an FPS-decorated title.
#define SDL_GetWindowTitle nitrogen_base_window_title
// Observe the existing host drain, preserving the guest DSP's sole owner.
#define spu_output_pop nitrogen_runtime_spu_output_pop
#define debug_server_shutdown nitrogen_runtime_shutdown
#define host_keymap_down nitrogen_host_keymap_down
#include NITROGEN_FRAMEWORK_MAIN
#undef host_keymap_down
#undef spu_output_pop
#undef debug_server_shutdown
#undef SDL_GetWindowTitle
#ifdef NITROGEN_WINDOW_DISPLAY
extern "C" int nitrogen_menu_reserved_height(void);
extern "C" void nitrogen_gpu_enable_ui(void);
// V2: the highest even-cadence Fullscreen refresh at the desktop resolution.
static float nitrogen_smooth_exclusive_hz(SDL_Window *window)
{
    const SDL_DisplayID display = SDL_GetDisplayForWindow(window);
    const SDL_DisplayMode *desktop = display ? SDL_GetDesktopDisplayMode(display) : nullptr;
    if (!desktop) return 0;
    int count = 0;
    SDL_DisplayMode **modes = SDL_GetFullscreenDisplayModes(display, &count);
    std::vector<float> rates;
    for (int i = 0; modes && i < count; ++i)
        if (modes[i]->w == desktop->w && modes[i]->h == desktop->h) rates.push_back(modes[i]->refresh_rate);
    SDL_free(modes);
    return nitrogen_window_smooth_refresh(rates.data(), (int)rates.size(), 60000.0 / 1001.0);
}
static void nitrogen_exclusive_refresh_env(float hz)
{
    // Read by the framework's own Fullscreen helper; it only accepts a rate
    // the display lists at the desktop resolution, otherwise the desktop's.
    char text[32] = "";
    if (hz > 0) std::snprintf(text, sizeof(text), "%.3f", hz);
#ifdef _WIN32
    _putenv_s("PSX_EXCLUSIVE_REFRESH_HZ", text);
#else
    if (*text) setenv("PSX_EXCLUSIVE_REFRESH_HZ", text, 1); else unsetenv("PSX_EXCLUSIVE_REFRESH_HZ");
#endif
}
// V4-b colour models on the OpenGL path. The framework's screen-colour LUT is
// applied only where the CPU converts 15-bit scanout; the OpenGL presentation
// never reaches it (measured: no pixel changed). This pass applies the same
// colorimetry on the GPU through the framework's post-effect chain: per
// channel gamma and luminance, the phosphor-to-sRGB matrix, the black lift
// and the sRGB curve. Constants come from the pinned framework's own model
// (runtime/src/color_lut.c, panel_model + rgb_to_rgb to sRGB).
static const char *const nitrogen_colour_glsl =
    "void main(){\n"
    "  vec3 c = clamp(texelFetch(u_src, ivec2(gl_FragCoord.xy) - u_org, 0).rgb, 0.0, 1.0);\n"
    "  vec3 lin = min(pow(c, vec3(u_model.x)) * u_model.y, vec3(1.0));\n"
    "  vec3 d = clamp(vec3(dot(u_row0, lin), dot(u_row1, lin), dot(u_row2, lin)), 0.0, 1.0);\n"
    "  vec3 v = u_model.z + (1.0 - u_model.z) * d;\n"
    "  vec3 lo = 12.92 * v, hi = 1.055 * pow(v, vec3(1.0 / 2.4)) - 0.055;\n"
    "  o = vec4(mix(hi, lo, vec3(lessThanEqual(v, vec3(0.0031308)))), 1.0);\n"
    "}\n";
struct NitrogenColourModel { const char *name; double gamma, luminance, black, m[3][3]; };
static const NitrogenColourModel nitrogen_colour_models[] = {
    {"crt", 2.4, 0.92, 0.004, {{0.939542064, 0.050181357, 0.010276579},
                               {0.017772223, 0.965792862, 0.016434914},
                               {-0.001621600, -0.004369750, 1.005991350}}},
    {"composite", 2.5, 0.90, 0.012, {{0.939542064, 0.050181357, 0.010276579},
                                     {0.017772223, 0.965792862, 0.016434914},
                                     {-0.001621600, -0.004369750, 1.005991350}}},
    {"trinitron", 2.35, 0.95, 0.002, {{1.068715855, -0.078609696, 0.009893841},
                                      {0.024106255, 0.960070928, 0.015822817},
                                      {0.001734353, 0.029741144, 0.968524502}}},
};
// Writes the shader and preset privately beside the executable (inside the
// portable folder) and returns the preset path, or "" when unavailable.
static std::string nitrogen_colour_preset(int screen)
{
    if (screen < NITROGEN_SCREEN_CRT || screen > NITROGEN_SCREEN_TRINITRON) return "";
    const NitrogenColourModel &m = nitrogen_colour_models[screen - NITROGEN_SCREEN_CRT];
    const char *base = SDL_GetBasePath();
    if (!base) return "";
    const std::filesystem::path dir = std::filesystem::u8path(std::string(base)) / "nitrogen-picture";
    std::error_code error;
    std::filesystem::create_directories(dir, error);
    char preset[768];
    std::snprintf(preset, sizeof(preset),
                  "pfx 1\npass colour_model\n  shader nitrogen-colour-model.glsl\n  input u_src source\n"
                  "  uniform u_model %.9f %.9f %.9f 0\n  uniform u_row0 %.9f %.9f %.9f\n"
                  "  uniform u_row1 %.9f %.9f %.9f\n  uniform u_row2 %.9f %.9f %.9f\n",
                  m.gamma, m.luminance, m.black, m.m[0][0], m.m[0][1], m.m[0][2],
                  m.m[1][0], m.m[1][1], m.m[1][2], m.m[2][0], m.m[2][1], m.m[2][2]);
    const std::filesystem::path pfx = dir / (std::string("colour-") + m.name + ".pfx");
    for (const auto &[path, text] : {std::pair<std::filesystem::path, std::string>{dir / "nitrogen-colour-model.glsl",
                                                                                   nitrogen_colour_glsl},
                                     std::pair<std::filesystem::path, std::string>{pfx, preset}}) {
        std::ifstream in(path, std::ios::binary);
        const std::string current((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
        if (current == text) continue;
        std::ofstream out(path, std::ios::binary | std::ios::trunc);
        out << text;
        if (!out) return "";
    }
    return pfx.u8string();
}
static void nitrogen_window_update()
{
    if(nitrogen_window_take_close()) {
        SDL_Event event{};event.type=SDL_EVENT_QUIT;SDL_PushEvent(&event);
    }
    // Game extras changed from F1: applied here, between guest frames, with
    // any renderer. Never headless or while a linked session could diverge.
    if (sdl_window && !g_headless && !psx_netplay_active()) nitrogen_extras_service();
    if (!g_gl_active || !sdl_window || g_headless) return;
    nitrogen_window_frame(s_frame_count);
    nitrogen_options_init(SDL_GetBasePath());
    nitrogen_window_ui_scale(nitrogen_option_get(NITROGEN_HUD_SCALE));
    nitrogen_window_hud_layout(nitrogen_option_get(NITROGEN_HUD_LAYOUT));
    static int applied_volume = -1;
    const int volume = nitrogen_option_get(NITROGEN_MASTER_VOLUME);
    if (applied_volume != volume) { host_volume_set(volume); applied_volume = volume; }
    static int applied_mode = -1;
    const int mode = nitrogen_option_get(NITROGEN_VIDEO_MODE);
    // V2 Fullscreen refresh. A developer PSX_EXCLUSIVE_REFRESH_HZ still wins.
    static const bool refresh_override = [] { const char *e = std::getenv("PSX_EXCLUSIVE_REFRESH_HZ"); return e && *e; }();
    static int applied_refresh = -1;
    const int refresh_choice = nitrogen_option_get(NITROGEN_FULLSCREEN_REFRESH);
    if (refresh_override) {
        if (applied_refresh != refresh_choice) nitrogen_window_report_refresh(refresh_choice, 0, 1);
        applied_refresh = refresh_choice;
    } else if (refresh_choice != applied_refresh || (mode == NITROGEN_FULLSCREEN && mode != applied_mode)) {
        const float hz = refresh_choice == NITROGEN_REFRESH_SMOOTH_60 ? nitrogen_smooth_exclusive_hz(sdl_window) : 0.0f;
        nitrogen_exclusive_refresh_env(hz);
        nitrogen_window_report_refresh(refresh_choice, hz, 0);
        // Already in Fullscreen: change its rate in place, as the helper would.
        if (applied_refresh >= 0 && refresh_choice != applied_refresh && s_window_fullscreen.mode == 2) {
            const SDL_DisplayID display = SDL_GetDisplayForWindow(sdl_window);
            const SDL_DisplayMode *desktop = display ? SDL_GetDesktopDisplayMode(display) : nullptr;
            SDL_DisplayMode exclusive{};
            if (desktop && SDL_GetClosestFullscreenDisplayMode(display, desktop->w, desktop->h,
                    hz > 0 ? hz : desktop->refresh_rate, true, &exclusive) &&
                exclusive.w == desktop->w && exclusive.h == desktop->h)
                (void)SDL_SetWindowFullscreenMode(sdl_window, &exclusive);
        }
        applied_refresh = refresh_choice;
    }
    if (applied_mode != mode) {
        if (psx_window_fullscreen_set(sdl_window, &s_window_fullscreen, mode) == 0) {
            g_fullscreen = mode;
            applied_mode = mode;
        } else {
            std::fprintf(stderr,"nitrogen: video mode %d unavailable (%s); keeping mode %d\n",mode,SDL_GetError(),s_window_fullscreen.mode);
            applied_mode=s_window_fullscreen.mode;
            (void)nitrogen_option_set(NITROGEN_VIDEO_MODE,applied_mode);
        }
    }
    // VSync: ON keeps the framework's startup choice, OFF presents at once.
    static int startup_vsync = g_video_vsync, applied_vsync = g_video_vsync;
    // A framework change (live settings, mod policy) becomes the new baseline.
    if (g_video_vsync != applied_vsync) startup_vsync = applied_vsync = g_video_vsync;
    const char *vsync_env = std::getenv("PSX_VSYNC");
    const int vsync = nitrogen_window_vsync_value(nitrogen_option_get(NITROGEN_VSYNC), startup_vsync,
                                                  vsync_env != nullptr, vsync_env ? std::atoi(vsync_env) : 0);
    if (vsync != applied_vsync) {
        g_video_vsync = vsync;
        apply_present_cadence();
        log_present_cadence();
        applied_vsync = vsync;
    }
    nitrogen_window_report_vsync(applied_vsync, vsync_env != nullptr);
    int window_w = 0, window_h = 0;
    SDL_GL_GetDrawableSize(sdl_window, &window_w, &window_h);
    window_h -= nitrogen_menu_reserved_height();
    if (window_w <= 0 || window_h <= 0) return; // minimized: keep the last valid frame
    // AUTO fills the window (the original behaviour); a fixed ratio uses the
    // largest centred rectangle of it, which the presentation letterboxes.
    const int aspect = nitrogen_option_get(NITROGEN_ASPECT);
    int width = window_w, height = window_h;
    nitrogen_window_game_area(aspect, window_w, window_h, &width, &height);
    nitrogen_window_report_window(window_w, window_h);
    nitrogen_window_publish(width, height);
    // The old title config may already exist after an update. Resolve the
    // requested sharp presentation here, rather than requiring a reimport.
    g_video_aa = false;
    g_video_fmv_filter = 0;
    // V4-a: Sharp (original) or Smooth world textures. The adapted renderer
    // keeps HUD, menus and 2D sprites sharp under Smooth.
    const int filter = nitrogen_option_get(NITROGEN_TEXTURE_FILTER) == NITROGEN_FILTER_SMOOTH ? 1 : 0;
    g_video_texfilter = filter;
    if (gr_texture_filter() != filter) gr_set_texture_filter(filter);
    // V4-b: each Picture setting is applied only when it changes. "Original"
    // restores the framework's own startup value, so defaults never touch it.
    static bool picture_ready = false;
    static int startup_dither = 1, startup_scan_on = 0;
    static std::string startup_post_shader;
    static float startup_scan_strength = .5f, startup_gamma = 1.f;
    static int applied_dither = NITROGEN_DITHER_ORIGINAL, applied_screen = NITROGEN_SCREEN_ORIGINAL;
    static int applied_scan = 0, applied_bright = 100;
    if (!picture_ready) {
        picture_ready = true;
        startup_dither = gl_renderer_dithering();
        startup_post_shader = g_video_post_shader;
        startup_scan_on = gl_renderer_get_scanlines(&startup_scan_strength);
        startup_gamma = gl_renderer_get_post_gamma();
    }
    const int dither = nitrogen_option_get(NITROGEN_DITHERING);
    if (dither != applied_dither) {
        gl_renderer_set_dithering(dither == NITROGEN_DITHER_OFF ? 0 : dither == NITROGEN_DITHER_FINE ? 2 : startup_dither);
        applied_dither = dither;
    }
    const int screen = nitrogen_option_get(NITROGEN_SCREEN_MODEL);
    if (screen != applied_screen) {
        // The colour pass uses the framework's post-effect chain; Original
        // restores the game's own configured chain (normally none).
        const std::string preset = nitrogen_colour_preset(screen);
        gl_renderer_set_post_shader(screen == NITROGEN_SCREEN_ORIGINAL || preset.empty()
                                    ? startup_post_shader.c_str() : preset.c_str());
        applied_screen = screen;
    }
    const int scan = nitrogen_option_get(NITROGEN_SCANLINES);
    if (scan != applied_scan) {
        if (scan) gl_renderer_set_scanlines(1, scan / 100.0f);
        else gl_renderer_set_scanlines(startup_scan_on, startup_scan_strength);
        applied_scan = scan;
    }
    const int bright = nitrogen_option_get(NITROGEN_BRIGHTNESS);
    if (bright != applied_bright) {
        gl_renderer_set_post_gamma(bright == 100 ? startup_gamma : bright / 100.0f);
        applied_bright = bright;
    }
    nitrogen_gpu_enable_ui();
    gpu_ws_set_auto_ui_in_place(0);
    int num = width, den = height;
    if (aspect == NITROGEN_ASPECT_AUTO) {
        const int divisor = aspect_gcd(num, den);
        num /= divisor; den /= divisor;
    } else {
        nitrogen_window_aspect_ratio(aspect, width, height, &num, &den);
    }
    // Project into the game's authored viewport, so its clipper receives
    // the wider field before rejecting polygons. Unprojected menu backdrop
    // sprites cover the drawable with a separate uniform transform.
    if (num != g_video_aspect_num || den != g_video_aspect_den ||
        g_ws_native_wide != 0) {
        g_video_aspect_num = num; g_video_aspect_den = den;
        g_ws_native_wide = 0;
        gl_renderer_set_display_aspect(num, den);
        g_ws_projection_mode = -1;
        refresh_widescreen_projection();
    }
    // Integer internal raster surfaces round upward to cover the drawable;
    // nearest presentation samples the result at the actual window size.
    static int last_scale = 0;
    static Uint32 last_window = 0;
    const Uint32 window_id = SDL_GetWindowID(sdl_window);
    if (last_window != window_id) {
        last_window = window_id;
        last_scale = 0;
        // The getter also takes renderer ownership. Set the fixed policy once
        // for a new window instead of draining the worker on every frame.
        (void)gl_renderer_set_post_aa(0);
    }
    GpuDisplayInfo display{}; gpu_get_display_info(&display);
    const int source_h=std::max(1,(int)display.height);
    const int source_w=std::max(1,(int)display.width);
    nitrogen_window_world_raster(width,height,source_w,source_h,nitrogen_option_get(NITROGEN_RENDER_PERCENT));
    const int viewport_w=(int64_t)num*3<(int64_t)den*4 ? height*4/3 : width;
    const int scale=std::max({1,(height+source_h-1)/source_h,
                            (viewport_w+source_w-1)/source_w});
    if (last_scale != scale) {
        (void)video_resolution_apply_live(std::min(scale * 240, PSX_IR_MAX_LINES), 0, 1000);
        last_scale = scale; // a refused request is retried on the next size step
    }
}
#endif
extern "C" uint32_t spu_output_pop(int16_t *, uint32_t);
extern "C" void debug_server_shutdown(void);
#ifdef PSX_HAS_OVERLAY_DISPATCH
extern "C" void psx_overlay_static_get_stats(uint64_t *, uint64_t *, uint64_t *, uint64_t *);
#endif
// The pinned main reaches this last step of every normal shutdown, including
// window-close paths that call exit(). Join GL before global UI mutexes die.
extern "C" void nitrogen_runtime_shutdown(void)
{
    gl_renderer_render_thread_stop();
    NitrogenRuntimeStatus status{};
    nitrogen_runtime_status(&status);
    std::fprintf(stdout, "nitrogen: last audio/render sample: worker=%d bound=%d rate=%d output_frames=%llu underflow_%s=%llu overflow_frames=%llu\n",
        status.worker_active, status.max_frames, status.host_rate,
        (unsigned long long)status.samples, status.legacy ? "events" : "samples",
        (unsigned long long)status.underruns, (unsigned long long)status.overflow_drops);
#ifdef PSX_HAS_OVERLAY_DISPATCH
    uint64_t checks = 0, hits = 0, variant_misses = 0, address_misses = 0;
    psx_overlay_static_get_stats(&checks, &hits, &variant_misses, &address_misses);
    std::fprintf(stdout, "nitrogen: native game slice: dispatches=%llu residency_rejections=%llu\n",
        (unsigned long long)hits, (unsigned long long)variant_misses);
#endif
    debug_server_shutdown();
}
namespace {
std::mutex runtime_status_mutex;
NitrogenRuntimeStatus runtime_status_snapshot{};
uint64_t runtime_sample_count = 0;
}
extern "C" void nitrogen_runtime_status(NitrogenRuntimeStatus *out)
{
    if (!out) return;
    std::lock_guard<std::mutex> lock(runtime_status_mutex);
    *out = runtime_status_snapshot;
}
extern "C" uint32_t nitrogen_runtime_spu_output_pop(int16_t *out, uint32_t count)
{
    const uint32_t copied = spu_output_pop(out, count);
    NitrogenRuntimeStatus status{};
    status.samples = runtime_sample_count += copied;
    double correction = 0;
    // The callback mutates bridge stats under this same device lock. UI
    // drawing must never read the bridge or a device being torn down.
    const bool lock_bridge = sdl_audio_device && !audio_legacy_mode();
    if (lock_bridge) psx_sdl_audio_lock(sdl_audio_device);
    status.audio_active = psx_audio_out_stats(&status.fill_ms, &status.target_ms,
        &status.underruns, &status.overflow_drops, &correction, &status.legacy, &status.host_rate);
    if (lock_bridge) psx_sdl_audio_unlock(sdl_audio_device);
    RtStats render{};
    rt_get_stats(&render); // Emulation-thread API; no context hand-off.
    status.worker_active = render.running;
    status.worker_held = render.held;
    status.max_frames = render.max_frames;
    status.frames_produced = render.frames_produced;
    status.frames_consumed = render.frames_consumed;
    status.backpressure_waits = render.backpressure_waits;
    { std::lock_guard<std::mutex> lock(runtime_status_mutex); runtime_status_snapshot = status; }
    return copied;
}
#include "../native/movie_player.h"
extern "C" void nitrogen_gpu_native_movie_complete(void);
namespace {
std::string movie_saved_title;
std::atomic<int> movie_active{0};
std::atomic<double> movie_clock{0};
std::atomic<uint64_t> movie_samples{0};
}
extern "C" int nitrogen_media_is_active(void) { return movie_active.load(); }
extern "C" void nitrogen_media_host_stats(double seconds, uint64_t samples) { movie_clock.store(seconds); movie_samples.store(samples); }
extern "C" void nitrogen_media_get_stats(double *seconds, uint64_t *samples) { *seconds = movie_clock.load(); *samples = movie_samples.load(); }
extern "C" float nitrogen_media_host_refresh(void)
{
    SDL_DisplayMode mode{};
    return SDL_GetCurrentDisplayMode(SDL_GetDisplayForWindow(sdl_window), &mode) == 0 && mode.refresh_rate > 0 ? mode.refresh_rate : 60.0f;
}
extern "C" int nitrogen_media_host_available(void) { return g_gl_active && sdl_window && !g_headless && !psx_netplay_active(); }
extern "C" int nitrogen_media_host_service(void)
{
    if (g_ovl_exit.load()) return -1;
#ifndef PSX_NO_DEBUG_TOOLS
    debug_server_poll();
#endif
#ifdef NITROGEN_WINDOW_DISPLAY
    nitrogen_window_update();
#endif
    host_pause_service(); return psx_host_pause_depth() > 0 ? 1 : 0;
}
extern "C" void nitrogen_media_host_quit(void) { g_ovl_exit = PSX_MENU_EXIT_QUIT + 1; }
extern "C" void nitrogen_media_host_devices(void) { refresh_player_devices(); }
extern "C" void nitrogen_media_host_begin(void)
{
    movie_active = 1; movie_clock = 0; movie_samples = 0;
    movie_saved_title = s_fps_base_title.empty() ? nitrogen_base_window_title(sdl_window) : s_fps_base_title;
    SDL_SetWindowTitle(sdl_window, "Nitrogen Squared development [Native video]");
    if (sdl_audio_device) psx_sdl_audio_set_paused(sdl_audio_device, 1);
}
extern "C" void nitrogen_media_host_end(void)
{
    movie_active = 0; nitrogen_gpu_native_movie_complete();
    if (sdl_audio_device) { psx_sdl_audio_clear(sdl_audio_device); psx_sdl_audio_set_paused(sdl_audio_device, psx_host_pause_depth() > 0); }
    g_audio_cycle_resync = 1; s_frame_pacer = FramePacer{0};
    s_fps_last_time = 0; s_fps_last_frame = 0; s_fps_base_title.clear();
    SDL_SetWindowTitle(sdl_window, movie_saved_title.c_str());
}
