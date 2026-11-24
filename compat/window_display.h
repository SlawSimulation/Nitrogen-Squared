/* Original window presentation policy; guest clocks remain unchanged. */
#ifndef NITROGEN_WINDOW_DISPLAY_H
#define NITROGEN_WINDOW_DISPLAY_H
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
/* The USA weapon preview's separately authored projection, observed through
 * the toolkit projection ring. World/menu projection centres are different. */
int nitrogen_window_projection_is_ui(int32_t ofx, int32_t ofy, int h);
/* Published by the identity-guarded title presenter, before its GTE calls. */
void nitrogen_window_menu_page(unsigned page);
void nitrogen_window_frame(uint64_t frame);
int nitrogen_window_controls_view(void);
int nitrogen_window_audio_view(void);
/* Title-screen hold feedback: 256 means hidden, 255..0 fades in. The caller
 * supplies monotonic seconds, independently of guest speed/frame count. */
unsigned nitrogen_window_close_hold(int held, double seconds);
int nitrogen_window_take_close(void);
int nitrogen_window_close_y(void);
/* Measured in drawable pixels, excluding the desktop menu. */
void nitrogen_window_publish(int width, int height);
void nitrogen_window_ui_vertex(float *x, float *y, unsigned flags,
                               int origin_x, int origin_y, int height,
                               int wide_width, int wide_offset);
int nitrogen_window_ui_step(int width, int height);
void nitrogen_window_ui_scale(int step); /* 0 = AUTO; otherwise 2,3,4,6 */
void nitrogen_window_world_raster(int width, int height, int source_width, int source_height, int percent);
void nitrogen_window_world_size(int *width, int *height);
/* Game-area shape. AUTO returns the window itself and its reduced ratio;
 * fixed ratios fit inside the window with the presentation letterbox's
 * integer arithmetic, so the published area equals the drawn rectangle. */
void nitrogen_window_aspect_ratio(int aspect, int width, int height, int *num, int *den);
void nitrogen_window_game_area(int aspect, int width, int height, int *area_w, int *area_h);
/* GL owner: the largest texture the driver accepts, and a world target the
 * driver refused to allocate. Later world sizes stay below both. */
void nitrogen_window_world_limit(int max_texture);
void nitrogen_window_world_failed(int width, int height);
enum { NITROGEN_WORLD_FULL, NITROGEN_WORLD_TEXTURE_LIMIT, NITROGEN_WORLD_ALLOCATION_LIMIT,
       NITROGEN_WORLD_NOT_USED };
typedef struct NitrogenWorldStatus {
    int percent, requested_w, requested_h, actual_w, actual_h, reason, max_texture;
} NitrogenWorldStatus;
void nitrogen_window_world_status(NitrogenWorldStatus *out);
/* The framework VSync value to use. ON keeps the framework's own startup
 * choice (which a mod or configuration may already have forced off); OFF is
 * immediate presentation. A developer PSX_VSYNC override always wins. */
int nitrogen_window_vsync_value(int option, int startup_value, int env_set, int env_value);
/* Reporting only: the window drawable, the published game area, and the
 * VSync value in effect (env_override = a developer PSX_VSYNC value won). */
void nitrogen_window_report_window(int width, int height);
void nitrogen_window_report_vsync(int value, int env_override);
void nitrogen_window_report(int *window_w, int *window_h, int *area_w, int *area_h,
                            int *vsync, int *env_override);
#ifdef __cplusplus
}
#endif
#endif
