/* Original host presentation preferences, shared by the in-game menus. */
#ifndef NITROGEN_GAME_OPTIONS_H
#define NITROGEN_GAME_OPTIONS_H
#ifdef __cplusplus
extern "C" {
#endif
enum { NITROGEN_WINDOWED, NITROGEN_BORDERLESS, NITROGEN_FULLSCREEN };
enum { NITROGEN_VIDEO_MODE, NITROGEN_HUD_SCALE, NITROGEN_RENDER_PERCENT, NITROGEN_MASTER_VOLUME,
       NITROGEN_ASPECT, NITROGEN_VSYNC, NITROGEN_HUD_LAYOUT, NITROGEN_FULLSCREEN_REFRESH,
       NITROGEN_TEXTURE_FILTER, NITROGEN_OPTION_COUNT };
/* Game-area shape inside the window. AUTO fills the window (the original
 * host behaviour); the others fit that exact ratio and add black bars. */
enum { NITROGEN_ASPECT_AUTO, NITROGEN_ASPECT_4_3, NITROGEN_ASPECT_16_9, NITROGEN_ASPECT_21_9,
       NITROGEN_ASPECT_32_9 };
/* Where edge-anchored HUD and menu parts sit. EDGES uses the game area's
 * edges (the original behaviour); CENTRED_16_9 uses a centred 16:9 region
 * when the game area is wider. Centre-anchored parts never move. */
enum { NITROGEN_HUD_EDGES, NITROGEN_HUD_CENTRED_16_9 };
/* Fullscreen (exclusive) display refresh. DESKTOP keeps the desktop's rate
 * (the original behaviour); SMOOTH_60 picks, at the desktop resolution, the
 * listed rate closest to the game's 59.94 Hz (the framework then paces the
 * game to it), else the highest whole multiple of it. Windowed and
 * borderless always use the desktop's rate. */
enum { NITROGEN_REFRESH_DESKTOP, NITROGEN_REFRESH_SMOOTH_60 };
/* V4 Picture: texture filtering. SHARP is the original look; SMOOTH filters
 * 3D world polygons only, so HUD, menus and 2D sprites keep exact texels. */
enum { NITROGEN_FILTER_SHARP, NITROGEN_FILTER_SMOOTH };
/* Call once on the main thread. A launcher-supplied path survives rebuilds. */
void nitrogen_options_init(const char *base_path);
int nitrogen_option_get(int option);
/* Invalid values are refused. Persist only an actual user change. */
int nitrogen_option_set(int option, int value);
int nitrogen_options_desktop(void);
#ifdef __cplusplus
}
#endif
#endif
