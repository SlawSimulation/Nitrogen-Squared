#include "window_display.h"
#include "nitrogen_options.h"
#include <algorithm>
#include <atomic>
#include <cmath>
namespace {
// One atomic pair: a render worker never sees half of a resize.
std::atomic<unsigned long long> drawable{0};
std::atomic<int> ui_scale{0};
std::atomic<unsigned long long> world_size{0};
std::atomic<int> world_texture_limit{0};
std::atomic<unsigned long long> world_refused{0};      // smallest refused w<<32|h
std::atomic<unsigned long long> world_requested{0};
std::atomic<int> world_percent{0}, world_reason{NITROGEN_WORLD_NOT_USED};
std::atomic<unsigned long long> window_size{0};
std::atomic<int> vsync_value{1}, vsync_env{0};
std::atomic<unsigned> menu_page{0};
std::atomic<uint64_t> host_frame{0}, menu_frame{0};
std::atomic<bool> close_requested{false};
bool closing=false, close_sent=false;
double close_start=0;
uint64_t close_frame=0;
}
extern "C" void nitrogen_window_frame(uint64_t frame) { host_frame.store(frame,std::memory_order_release); }
extern "C" void nitrogen_window_menu_page(unsigned page)
{
    menu_frame.store(host_frame.load(std::memory_order_relaxed),std::memory_order_relaxed);
    menu_page.store(page,std::memory_order_release);
}
static int menu_view(unsigned page)
{
    if(menu_page.load(std::memory_order_acquire)!=page) return 0;
    const auto now=host_frame.load(std::memory_order_acquire),at=menu_frame.load(std::memory_order_relaxed);
    // A GPU DMA may consume the previous guest frame. Expire the title-owned
    // context if a reset/restore/other occupant stops calling its presenter.
    return now>=at && now-at<=1;
}
extern "C" int nitrogen_window_controls_view(void) { return menu_view(8); }
extern "C" int nitrogen_window_audio_view(void) { return menu_view(19); }
extern "C" unsigned nitrogen_window_close_hold(int held,double seconds)
{
    if(!held || !std::isfinite(seconds)) {closing=false;close_sent=false;return 256;}
    const uint64_t frame=host_frame.load(std::memory_order_acquire);
    if(frame<close_frame || frame-close_frame>2) closing=false;
    close_frame=frame;
    if(!closing || seconds<close_start) {closing=true;close_start=seconds;}
    const double elapsed=seconds-close_start;
    if(elapsed>=2 && !close_sent) {close_requested.store(true,std::memory_order_release);close_sent=true;}
    return static_cast<unsigned>(255*(1-std::min(1.0,elapsed)));
}
extern "C" int nitrogen_window_take_close(void)
{ return close_requested.exchange(false,std::memory_order_acq_rel); }
extern "C" int nitrogen_window_close_y(void)
{
    const auto size=drawable.load(std::memory_order_acquire);
    const int w=static_cast<int>(size>>32),h=static_cast<unsigned>(size);
    // Centre the 16-pixel font at one third of the actual game area.
    return h>0 ? 112-h/(6*nitrogen_window_ui_step(w,h)) : 72;
}
extern "C" int nitrogen_window_projection_is_ui(int32_t ofx,int32_t ofy,int h)
{
    if(ofx==40*65536 && ofy==199*65536 && h==320) return 1;
    return nitrogen_window_controls_view() && ofx==184*65536 && ofy==120*65536 && h==320;
}
extern "C" void nitrogen_window_publish(int width, int height)
{
    drawable.store((static_cast<unsigned long long>(std::max(0, width)) << 32) |
                   static_cast<unsigned>(std::max(0, height)), std::memory_order_release);
}
extern "C" int nitrogen_window_ui_step(int width, int height)
{
    const int manual = ui_scale.load(std::memory_order_acquire);
    if (manual) return manual;
    // Square source pixels, with the owner's requested 2x minimum.
    return std::max(2, std::min(width / 365, height / 240));
}
extern "C" void nitrogen_window_ui_scale(int step)
{
    if (step == 0 || step == 2 || step == 3 || step == 4 || step == 6)
        ui_scale.store(step, std::memory_order_release);
}
static unsigned long long pack(unsigned long long w,unsigned long long h) { return (w<<32)|h; }
// Scale (w,h) uniformly so neither exceeds (lw,lh); unchanged when it fits.
static void fit_within(unsigned &w,unsigned &h,unsigned lw,unsigned lh)
{
    if (w<=lw && h<=lh) return;
    // Compare w/lw with h/lh without floating point: the larger ratio binds.
    if (static_cast<unsigned long long>(w)*lh >= static_cast<unsigned long long>(h)*lw) {
        h=static_cast<unsigned>(static_cast<unsigned long long>(h)*lw/w); w=lw;
    } else {
        w=static_cast<unsigned>(static_cast<unsigned long long>(w)*lh/h); h=lh;
    }
    w=std::max(1u,w); h=std::max(1u,h);
}
extern "C" void nitrogen_window_world_raster(int width,int height,int sw,int sh,int percent)
{
    world_percent.store(percent,std::memory_order_relaxed);
    if (sw!=364 || sh<1 || width<1 || height<1) {
        world_requested=0; world_size=0; world_reason=NITROGEN_WORLD_NOT_USED; return;
    }
    const int viewport_w=static_cast<long long>(width)*3<static_cast<long long>(height)*4?height*4/3:width;
    unsigned w=static_cast<unsigned>((static_cast<long long>(viewport_w)*percent*1024+100*sw-1)/(100*sw));
    unsigned h=static_cast<unsigned>((static_cast<long long>(height)*percent*512+100*sh-1)/(100*sh));
    world_requested.store(pack(w,h),std::memory_order_relaxed);
    int reason=NITROGEN_WORLD_FULL;
    // The largest proportional size the driver accepts, never a silent 100%.
    const int limit=world_texture_limit.load(std::memory_order_acquire);
    if (limit>0 && (w>static_cast<unsigned>(limit) || h>static_cast<unsigned>(limit))) {
        fit_within(w,h,static_cast<unsigned>(limit),static_cast<unsigned>(limit));
        reason=NITROGEN_WORLD_TEXTURE_LIMIT;
    }
    const auto refused=world_refused.load(std::memory_order_acquire);
    if (refused) {
        // Step below the refused size: 90% of it, in each dimension.
        const unsigned rw=static_cast<unsigned>(refused>>32)*9/10, rh=static_cast<unsigned>(refused)*9/10;
        if (w>=rw || h>=rh) {
            fit_within(w,h,std::max(1u,rw-1),std::max(1u,rh-1));
            reason=NITROGEN_WORLD_ALLOCATION_LIMIT;
        }
    }
    world_reason.store(reason,std::memory_order_relaxed);
    world_size.store(pack(w,h),std::memory_order_release);
}
extern "C" void nitrogen_window_world_limit(int max_texture)
{ if (max_texture>0) world_texture_limit.store(max_texture,std::memory_order_release); }
extern "C" void nitrogen_window_world_failed(int width,int height)
{
    if (width<1 || height<1) return;
    auto old=world_refused.load(std::memory_order_acquire);
    const auto now=pack(static_cast<unsigned>(width),static_cast<unsigned>(height));
    // Keep the smallest refusal: a later, larger request cannot raise the cap.
    while ((!old || static_cast<unsigned long long>(width)*height <
                    (old>>32)*static_cast<unsigned>(old)) &&
           !world_refused.compare_exchange_weak(old,now,std::memory_order_acq_rel)) {}
}
extern "C" void nitrogen_window_world_status(NitrogenWorldStatus *out)
{
    if (!out) return;
    const auto req=world_requested.load(std::memory_order_relaxed), act=world_size.load(std::memory_order_acquire);
    out->percent=world_percent.load(std::memory_order_relaxed);
    out->requested_w=static_cast<int>(req>>32); out->requested_h=static_cast<int>(static_cast<unsigned>(req));
    out->actual_w=static_cast<int>(act>>32); out->actual_h=static_cast<int>(static_cast<unsigned>(act));
    out->reason=world_reason.load(std::memory_order_relaxed);
    out->max_texture=world_texture_limit.load(std::memory_order_acquire);
}
extern "C" void nitrogen_window_report_window(int width,int height)
{ window_size.store(pack(static_cast<unsigned>(std::max(0,width)),static_cast<unsigned>(std::max(0,height))),std::memory_order_release); }
extern "C" void nitrogen_window_report_vsync(int value,int env_override)
{ vsync_value.store(value,std::memory_order_relaxed); vsync_env.store(env_override?1:0,std::memory_order_relaxed); }
extern "C" void nitrogen_window_report(int *ww,int *wh,int *aw,int *ah,int *vsync,int *env_override)
{
    const auto win=window_size.load(std::memory_order_acquire), area=drawable.load(std::memory_order_acquire);
    *ww=static_cast<int>(win>>32); *wh=static_cast<int>(static_cast<unsigned>(win));
    *aw=static_cast<int>(area>>32); *ah=static_cast<int>(static_cast<unsigned>(area));
    *vsync=vsync_value.load(std::memory_order_relaxed); *env_override=vsync_env.load(std::memory_order_relaxed);
}
extern "C" int nitrogen_window_vsync_value(int option,int startup,int env_set,int env_value)
{
    if (env_set) return env_value;
    return option==0 ? 0 : startup;
}
extern "C" void nitrogen_window_aspect_ratio(int aspect,int width,int height,int *num,int *den)
{
    static const int ratios[][2]{{4,3},{16,9},{21,9},{32,9}};
    if (aspect>=NITROGEN_ASPECT_4_3 && aspect<=NITROGEN_ASPECT_32_9) {
        *num=ratios[aspect-1][0]; *den=ratios[aspect-1][1]; return;
    }
    int a=std::max(1,width), b=std::max(1,height);
    while (b) { const int t=a%b; a=b; b=t; }
    *num=std::max(1,width)/a; *den=std::max(1,height)/a;
}
extern "C" void nitrogen_window_game_area(int aspect,int width,int height,int *area_w,int *area_h)
{
    if (aspect<NITROGEN_ASPECT_4_3 || aspect>NITROGEN_ASPECT_32_9 || width<1 || height<1) {
        *area_w=width; *area_h=height; return;
    }
    int num=1,den=1; nitrogen_window_aspect_ratio(aspect,width,height,&num,&den);
    // Identical to nitrogen_menu_letterbox's fit of the usable height.
    int dw=width, dh=static_cast<int>(static_cast<long long>(width)*den/num);
    if (dh>height) { dh=height; dw=static_cast<int>(static_cast<long long>(height)*num/den); }
    *area_w=dw; *area_h=dh;
}
extern "C" void nitrogen_window_world_size(int *width,int *height)
{
    const auto size=world_size.load(std::memory_order_acquire);
    *width=static_cast<int>(size>>32); *height=static_cast<unsigned>(size);
}
extern "C" void nitrogen_window_ui_vertex(float *x, float *y, unsigned flags,
                                           int ox, int oy, int height,
                                           int wide_width, int wide_offset)
{
    (void)height; (void)wide_width; (void)wide_offset;
    const bool ui = (flags & 0x10u) != 0;
    const unsigned role=(flags>>5)&7u;
    if (!ui && role!=6) return;
    const auto size=drawable.load(std::memory_order_acquire);
    const int dw=static_cast<int>(size>>32), dh=static_cast<unsigned>(size);
    const int source_h=(flags>>8)&255u;
    if (dw<1 || dh<1 || source_h<1) return;
    if (!ui) {
        if (static_cast<long long>(dw)*3>static_cast<long long>(dh)*4) {
            const float cover=static_cast<float>(dw)*3.f/(dh*4.f);
            *y=oy+source_h*.5f+(*y-oy-source_h*.5f)*cover;
        }
        return;
    }
    const float ax=(role==1 || role==3) ? 0.f : role==4 ? 1.f : .5f;
    const float ay=(role==1 || role==2) ? 0.f : (role==3 || role==4 || role==5) ? 1.f : .5f;
    const int step=nitrogen_window_ui_step(dw,dh);
    const int viewport_w=static_cast<long long>(dw)*3<static_cast<long long>(dh)*4 ? dh*4/3 : dw;
    // Map square-pixel UI through the actual present/crop, independently from
    // world projection. All parts of a role use the authored 368x240 pivot.
    const int viewport_x=(dw-viewport_w)/2;
    *x=ox+(std::floor(ax*dw)+(*x-ox-ax*368.f)*step-viewport_x)*364.f/viewport_w;
    *y=oy+(std::floor(ay*dh)+(*y-oy-ay*240.f)*step)*source_h/dh;
}
