#include "window_display.h"
#include "nitrogen_options.h"
#include <algorithm>
#include <atomic>
#include <cmath>
namespace {
// One atomic pair: a render worker never sees half of a resize.
std::atomic<unsigned long long> drawable{0};
std::atomic<int> ui_scale{0};
std::atomic<int> hud_layout{0};
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
extern "C" void nitrogen_window_hud_layout(int layout)
{
    if (layout == NITROGEN_HUD_EDGES || layout == NITROGEN_HUD_CENTRED_16_9)
        hud_layout.store(layout, std::memory_order_release);
}
extern "C" void nitrogen_window_hud_span(int width,int height,int *x,int *span_w)
{
    *x = 0; *span_w = width;
    if (hud_layout.load(std::memory_order_acquire) != NITROGEN_HUD_CENTRED_16_9 || width<1 || height<1) return;
    const int region = static_cast<int>(static_cast<long long>(height)*16/9);
    if (region >= width) return;        // 16:9 or narrower: identical to EDGES
    *span_w = region; *x = (width-region)/2;
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
    // Edge anchors follow the HUD span; centre anchors keep floor(dw/2) so
    // centred phrases and menus are identical in both layouts.
    int span_x=0, span_w=dw;
    nitrogen_window_hud_span(dw,dh,&span_x,&span_w);
    const float anchor=ax==.5f ? std::floor(ax*dw) : span_x+std::floor(ax*span_w);
    // Map square-pixel UI through the actual present/crop, independently from
    // world projection. All parts of a role use the authored 368x240 pivot.
    const int viewport_x=(dw-viewport_w)/2;
    *x=ox+(anchor+(*x-ox-ax*368.f)*step-viewport_x)*364.f/viewport_w;
    *y=oy+(std::floor(ay*dh)+(*y-oy-ay*240.f)*step)*source_h/dh;
}
// V1.2 game extras. Requests and the published snapshot are independent
// atomics: the desktop menu never touches guest memory itself.
namespace {
constexpr int extra_choices[NITROGEN_EXTRA_COUNT]{3,3,2,2,2};
std::atomic<int> extra_request[NITROGEN_EXTRA_COUNT]{{-1},{-1},{-1},{-1},{-1}};
std::atomic<int> extra_value[NITROGEN_EXTRA_COUNT]{};
std::atomic<int> extra_available{0};
std::atomic<void (*)(void)> extra_service{nullptr};
bool extra_valid(int extra,int value)
{
    return extra>=0 && extra<NITROGEN_EXTRA_COUNT && value>=0 && value<extra_choices[extra];
}
}
extern "C" int nitrogen_extras_choices(int extra)
{
    return extra>=0 && extra<NITROGEN_EXTRA_COUNT ? extra_choices[extra] : 0;
}
extern "C" void nitrogen_extras_request(int extra,int value)
{
    if (extra_valid(extra,value)) extra_request[extra].store(value,std::memory_order_release);
}
extern "C" int nitrogen_extras_take(int extra)
{
    return extra>=0 && extra<NITROGEN_EXTRA_COUNT ? extra_request[extra].exchange(-1,std::memory_order_acq_rel) : -1;
}
extern "C" void nitrogen_extras_publish(int available,const int *values)
{
    for (int i=0;i<NITROGEN_EXTRA_COUNT;++i)
        extra_value[i].store(available && extra_valid(i,values[i]) ? values[i] : 0,std::memory_order_relaxed);
    extra_available.store(available?1:0,std::memory_order_release);
}
extern "C" int nitrogen_extras_snapshot(int *values)
{
    const int available=extra_available.load(std::memory_order_acquire);
    for (int i=0;i<NITROGEN_EXTRA_COUNT;++i) {
        const int pending=extra_request[i].load(std::memory_order_acquire);
        values[i]=pending>=0 ? pending : extra_value[i].load(std::memory_order_relaxed);
    }
    return available;
}
extern "C" void nitrogen_extras_set_service(void (*service)(void)) { extra_service.store(service,std::memory_order_release); }
extern "C" void nitrogen_extras_service(void)
{
    if (auto service=extra_service.load(std::memory_order_acquire)) service();
    else {
        int none[NITROGEN_EXTRA_COUNT]{};
        for (int i=0;i<NITROGEN_EXTRA_COUNT;++i) (void)nitrogen_extras_take(i);
        nitrogen_extras_publish(0,none);
    }
}
// V2 frame pacing helpers.
extern "C" float nitrogen_window_smooth_refresh(const float *rates,int count,double guest_hz)
{
    float best=0;
    if (!rates || guest_hz<=0) return 0;
    for (int i=0;i<count;++i) {
        const double r=rates[i];
        if (!(r>0)) continue;
        const long k=std::lround(r/guest_hz);
        if (k>=1 && std::fabs(r/k-guest_hz)<=guest_hz*0.005 && rates[i]>best) best=rates[i];
    }
    return best;
}
extern "C" double nitrogen_window_refreshes_per_frame(double host_hz,double guest_hz,int *even)
{
    if (even) *even=0;
    if (!(host_hz>0) || !(guest_hz>0)) return 0;
    const long k=std::lround(host_hz/guest_hz);
    if (even) *even=k>=1 && std::fabs(host_hz/k-guest_hz)<=guest_hz*0.005;
    return host_hz/guest_hz;
}
namespace {
std::atomic<int> refresh_choice{0}, refresh_override{0};
std::atomic<float> refresh_hz{0};
}
extern "C" void nitrogen_window_report_refresh(int choice,float hz,int override)
{
    refresh_hz.store(hz,std::memory_order_relaxed);
    refresh_override.store(override,std::memory_order_relaxed);
    refresh_choice.store(choice,std::memory_order_release);
}
extern "C" void nitrogen_window_refresh_report(int *choice,float *hz,int *override)
{
    *choice=refresh_choice.load(std::memory_order_acquire);
    *hz=refresh_hz.load(std::memory_order_relaxed);
    *override=refresh_override.load(std::memory_order_relaxed);
}
