"""Display V1: aspect areas, 3D-size clamping, VSync policy and option persistence.

Original synthetic fixtures; no game data, GPU or window is used.
"""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPILER = shutil.which('c++')


def run_fixture(test, source, sources, args=(), env=None):
    with tempfile.TemporaryDirectory() as temp:
        p = Path(temp)
        (p / 'fixture.cpp').write_text(source)
        built = subprocess.run(['c++', '-std=c++17', '-I', str(ROOT / 'compat'), str(p / 'fixture.cpp'),
                                *[str(ROOT / 'compat' / s) for s in sources], '-o', str(p / 'fixture')],
                               capture_output=True, text=True)
        test.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        ran = subprocess.run([str(p / 'fixture'), *args], capture_output=True, text=True, env=env)
        test.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        return ran.stdout


@unittest.skipUnless(COMPILER, 'C++ compiler required for display fixtures')
class AspectAndClampTests(unittest.TestCase):
    def test_game_area_matches_the_presentation_letterbox_exactly(self):
        run_fixture(self, r'''
#include "window_display.h"
#include "nitrogen_options.h"
#include <cassert>
#include <cstdint>
#include <cstdlib>
// The presentation's fit (nitrogen_menu_letterbox with no menu bar reserved).
static void letterbox(int ww,int wh,int num,int den,int*w,int*h){
 int dw=ww,dh=(int)((int64_t)ww*den/num);
 if(dh>wh){dh=wh;dw=(int)((int64_t)wh*num/den);}
 *w=dw;*h=dh;
}
int main(){
 int n=0,d=0,w=0,h=0;
 // AUTO is the window itself, reduced like the original aspect_gcd.
 nitrogen_window_aspect_ratio(NITROGEN_ASPECT_AUTO,3840,1080,&n,&d);assert(n==32&&d==9);
 nitrogen_window_aspect_ratio(NITROGEN_ASPECT_AUTO,1920,1080,&n,&d);assert(n==16&&d==9);
 nitrogen_window_aspect_ratio(NITROGEN_ASPECT_AUTO,1366,768,&n,&d);assert(n==683&&d==384);
 nitrogen_window_game_area(NITROGEN_ASPECT_AUTO,3840,1080,&w,&h);assert(w==3840&&h==1080);
 nitrogen_window_game_area(99,3840,1080,&w,&h);assert(w==3840&&h==1080); // unknown = AUTO
 // Exact ratios: 21:9 stays 21:9, never 64:27.
 const int ratio[][2]={{4,3},{16,9},{21,9},{32,9}};
 for(int a=NITROGEN_ASPECT_4_3;a<=NITROGEN_ASPECT_32_9;a++){
  nitrogen_window_aspect_ratio(a,3840,1080,&n,&d);
  assert(n==ratio[a-1][0]&&d==ratio[a-1][1]);
 }
 // The owner's 49-inch desktop.
 nitrogen_window_game_area(NITROGEN_ASPECT_4_3,3840,1080,&w,&h);assert(w==1440&&h==1080);
 nitrogen_window_game_area(NITROGEN_ASPECT_16_9,3840,1080,&w,&h);assert(w==1920&&h==1080);
 nitrogen_window_game_area(NITROGEN_ASPECT_21_9,3840,1080,&w,&h);assert(w==2520&&h==1080);
 nitrogen_window_game_area(NITROGEN_ASPECT_32_9,3840,1080,&w,&h);assert(w==3840&&h==1080);
 // A wider ratio than the window: bars above and below.
 nitrogen_window_game_area(NITROGEN_ASPECT_32_9,1920,1080,&w,&h);assert(w==1920&&h==540);
 nitrogen_window_game_area(NITROGEN_ASPECT_21_9,2560,1440,&w,&h);assert(w==2560&&h==1097);
 // Every size: identical to the letterbox, inside the window, within a pixel of the ratio.
 for(int ww=320;ww<=7680;ww+=97)for(int wh=200;wh<=4320;wh+=61)
  for(int a=NITROGEN_ASPECT_4_3;a<=NITROGEN_ASPECT_32_9;a++){
   int lw=0,lh=0;
   nitrogen_window_game_area(a,ww,wh,&w,&h);
   letterbox(ww,wh,ratio[a-1][0],ratio[a-1][1],&lw,&lh);
   assert(w==lw&&h==lh);
   assert(w<=ww&&h<=wh&&(w==ww||h==wh));
   assert(std::llabs((int64_t)w*ratio[a-1][1]-(int64_t)h*ratio[a-1][0])<=ratio[a-1][0]+ratio[a-1][1]);
  }
}
''', ['window_display.cpp'])

    def test_world_raster_uses_the_largest_supported_size_and_reports_why(self):
        run_fixture(self, r'''
#include "window_display.h"
#include <cassert>
#include <cstdint>
static NitrogenWorldStatus status(){NitrogenWorldStatus s{};nitrogen_window_world_status(&s);return s;}
int main(){
 // Before the GL owner reports a limit, the requested size is used.
 nitrogen_window_world_raster(1920,1080,364,240,100);
 NitrogenWorldStatus s=status();
 assert(s.reason==NITROGEN_WORLD_FULL&&s.percent==100);
 assert(s.actual_w==s.requested_w&&s.actual_h==s.requested_h);
 assert(s.requested_w==5402&&s.requested_h==2304);
 nitrogen_window_world_limit(32768);
 // 5120x1440 at 200% fits the 1080 Ti's limit: unchanged.
 nitrogen_window_world_raster(5120,1440,364,240,200);
 s=status();assert(s.reason==NITROGEN_WORLD_FULL&&s.actual_w==28808&&s.actual_h==6144);
 // 7680x2160 at 200% (43211 wide) does not: the largest proportional size, not 100%.
 nitrogen_window_world_raster(7680,2160,364,240,200);
 s=status();
 assert(s.reason==NITROGEN_WORLD_TEXTURE_LIMIT&&s.max_texture==32768);
 assert(s.requested_w==43211&&s.requested_h==9216);
 assert(s.actual_w==32768&&s.actual_h<=32768);
 assert(s.actual_h==(int)((int64_t)9216*32768/43211));
 int w=0,h=0;nitrogen_window_world_size(&w,&h);assert(w==s.actual_w&&h==s.actual_h);
 assert(s.actual_w>21606); // well above what 100% would give
 // A refused allocation: later sizes step below it, and the smallest refusal is kept.
 nitrogen_window_world_failed(28808,6144);
 nitrogen_window_world_raster(5120,1440,364,240,200);
 s=status();
 assert(s.reason==NITROGEN_WORLD_ALLOCATION_LIMIT);
 assert(s.actual_w<28808*9/10&&s.actual_h<6144*9/10&&s.actual_w>0);
 nitrogen_window_world_failed(43000,9000); // larger: does not raise the cap
 nitrogen_window_world_raster(5120,1440,364,240,200);
 NitrogenWorldStatus again=status();assert(again.actual_w==s.actual_w&&again.actual_h==s.actual_h);
 // Smaller requests below the cap are untouched.
 nitrogen_window_world_raster(1920,1080,364,240,100);
 s=status();assert(s.reason==NITROGEN_WORLD_FULL&&s.actual_w==5402);
 // Movies and 2D images do not use the world raster.
 nitrogen_window_world_raster(1920,1080,320,240,200);
 s=status();assert(s.reason==NITROGEN_WORLD_NOT_USED&&s.actual_w==0);
 nitrogen_window_world_size(&w,&h);assert(w==0&&h==0);
}
''', ['window_display.cpp'])

    def test_vsync_policy_and_reporting(self):
        run_fixture(self, r'''
#include "window_display.h"
#include <cassert>
int main(){
 assert(nitrogen_window_vsync_value(1,1,0,0)==1);   // default: unchanged
 assert(nitrogen_window_vsync_value(0,1,0,0)==0);   // Off
 assert(nitrogen_window_vsync_value(1,0,0,0)==0);   // framework/mod forced off stays off
 assert(nitrogen_window_vsync_value(1,-1,0,0)==-1); // framework's adaptive choice is kept
 assert(nitrogen_window_vsync_value(0,1,1,1)==1);   // developer PSX_VSYNC wins
 assert(nitrogen_window_vsync_value(1,1,1,0)==0);
 nitrogen_window_report_window(3840,1080);nitrogen_window_publish(1440,1080);nitrogen_window_report_vsync(0,1);
 int ww,wh,aw,ah,v,env;nitrogen_window_report(&ww,&wh,&aw,&ah,&v,&env);
 assert(ww==3840&&wh==1080&&aw==1440&&ah==1080&&v==0&&env==1);
}
''', ['window_display.cpp'])

    def test_hud_position_centred_moves_only_edge_anchors_into_a_16_9_region(self):
        run_fixture(self, r"""
#include "window_display.h"
#include "nitrogen_options.h"
#include <cassert>
#include <cmath>
#include <initializer_list>
// Screen column of a UI vertex after the window mapping (game area dw wide).
static float screen_x(float x,int role,int dw,int dh){
 nitrogen_window_publish(dw,dh);
 float y=120;nitrogen_window_ui_vertex(&x,&y,0x10u|(unsigned(role)<<5)|(240u<<8),0,0,240,0,0);
 const int vw=(long long)dw*3<(long long)dh*4?dh*4/3:dw;
 return x*vw/364.f+(dw-vw)/2;
}
static bool near(float a,float b){return std::fabs(a-b)<.01f;}
int main(){
 int x=0,w=0;
 // Edges (default): the original anchors, the whole game area.
 nitrogen_window_hud_span(3840,1080,&x,&w);assert(x==0&&w==3840);
 const int step=nitrogen_window_ui_step(3840,1080);assert(step==4);
 const float left=screen_x(10,1,3840,1080),right=screen_x(358,4,3840,1080),mid=screen_x(184,0,3840,1080);
 const float bottom_left=screen_x(10,3,3840,1080);
 assert(near(left,40)&&near(right,3800)&&near(mid,1920)&&near(bottom_left,40));
 // Centred: a 1920-wide region in the middle of 3840x1080.
 nitrogen_window_hud_layout(NITROGEN_HUD_CENTRED_16_9);
 nitrogen_window_hud_span(3840,1080,&x,&w);assert(x==960&&w==1920);
 assert(near(screen_x(10,1,3840,1080),1000)&&near(screen_x(358,4,3840,1080),2840));
 assert(near(screen_x(10,3,3840,1080),1000));
 assert(screen_x(184,0,3840,1080)==mid);          // centred phrases never move
 nitrogen_window_hud_span(2520,1080,&x,&w);assert(x==300&&w==1920);  // 21:9 game area
 // 16:9 or narrower game areas: centred is byte-for-byte the original placement.
 for(int dw:{1920,1440,1280,800})for(int role:{0,1,2,3,4,5})for(float vx:{0.f,10.f,184.f,358.f,368.f}){
  nitrogen_window_hud_layout(NITROGEN_HUD_EDGES);const float a=screen_x(vx,role,dw,1080);
  nitrogen_window_hud_layout(NITROGEN_HUD_CENTRED_16_9);const float b=screen_x(vx,role,dw,1080);
  assert(a==b);
 }
 // An invalid value is ignored, not applied.
 nitrogen_window_hud_layout(7);nitrogen_window_hud_span(3840,1080,&x,&w);assert(x==960&&w==1920);
 nitrogen_window_hud_layout(NITROGEN_HUD_EDGES);nitrogen_window_hud_span(3840,1080,&x,&w);assert(x==0&&w==3840);
}
""", ['window_display.cpp'])

    def test_options_persist_aspect_and_vsync_with_auto_and_on_defaults(self):
        source = r'''
#include "nitrogen_options.h"
#include <cassert>
#include <cstdlib>
#include <cstring>
int main(int argc,char**argv){
 nitrogen_options_init(argv[1]);
 const int aspect=std::atoi(argv[2]),vsync=std::atoi(argv[3]);
 assert(nitrogen_option_get(NITROGEN_ASPECT)==aspect);
 assert(nitrogen_option_get(NITROGEN_VSYNC)==vsync);
 assert(nitrogen_option_get(NITROGEN_HUD_LAYOUT)==(argc>4&&!std::strcmp(argv[4],"hud")));
 if(argc>4&&!std::strcmp(argv[4],"write")){
  assert(!nitrogen_option_set(NITROGEN_ASPECT,5));
  assert(!nitrogen_option_set(NITROGEN_ASPECT,-1));
  assert(!nitrogen_option_set(NITROGEN_VSYNC,2));
  assert(nitrogen_option_set(NITROGEN_ASPECT,NITROGEN_ASPECT_21_9));
  assert(nitrogen_option_set(NITROGEN_VSYNC,0));
  assert(nitrogen_option_get(NITROGEN_HUD_LAYOUT)==NITROGEN_HUD_EDGES);   // default: original layout
  assert(!nitrogen_option_set(NITROGEN_HUD_LAYOUT,2));
 }
}
'''
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'nitrogen-options.ini'
            env = dict(os.environ, NITROGEN_OPTIONS_FILE=str(config))
            # No file, then a file written before V1: Auto (0) and On (1).
            run_fixture(self, source, ['game_options.cpp'], [temp + '/', '0', '1'], env)
            config.write_text('video_mode 0\nhud_scale 0\nrender_percent 100\nmaster_volume 80\n')
            run_fixture(self, source, ['game_options.cpp'], [temp + '/', '0', '1', 'write'], env)
            text = config.read_text()
            self.assertIn('aspect 3\n', text)
            self.assertIn('vsync 0\n', text)
            self.assertIn('master_volume 80\n', text)
            self.assertIn('hud_layout 0\n', text)
            config.write_text(text.replace('hud_layout 0', 'hud_layout 1'))
            run_fixture(self, source, ['game_options.cpp'], [temp + '/', '3', '0', 'hud'], env)
            config.write_text(text)   # back to the saved Edges layout
            run_fixture(self, source, ['game_options.cpp'], [temp + '/', '3', '0'], env)
            # Invalid stored values are ignored, never applied.
            config.write_text('aspect 7\nvsync 2\nhud_layout 5\n')
            run_fixture(self, source, ['game_options.cpp'], [temp + '/', '0', '1'], env)


class VsyncWiringTests(unittest.TestCase):
    def test_runtime_applies_vsync_live_and_keeps_auto_on_the_original_path(self):
        host = (ROOT / 'compat' / 'runtime_host.cpp').read_text(encoding='utf-8')
        update = host[host.index('static void nitrogen_window_update()\n{'):]
        update = update[:update.index('\n}\n')]
        # A changed value reaches the framework's own present-cadence switch.
        change = update[update.index('if (vsync != applied_vsync)'):]
        change = change[:change.index('}')]
        self.assertIn('g_video_vsync = vsync;', change)
        self.assertIn('apply_present_cadence();', change)
        self.assertIn('PSX_VSYNC', update)
        # AUTO keeps the original gcd-reduced window ratio.
        auto = update[update.index('if (aspect == NITROGEN_ASPECT_AUTO) {'):]
        self.assertIn('aspect_gcd(num, den)', auto[:auto.index('} else {')])
        self.assertTrue(re.search(r'nitrogen_window_game_area\(aspect, window_w, window_h, &width, &height\)', update))
        self.assertIn('nitrogen_window_world_limit(limit);',
                      (ROOT / 'compat' / 'world_raster.inc').read_text(encoding='utf-8'))


@unittest.skipUnless(COMPILER, 'C++ compiler required for display fixtures')
class GameExtrasQueueTests(unittest.TestCase):
    def test_requests_are_validated_queued_and_dropped_without_a_game_service(self):
        run_fixture(self, r"""
#include "window_display.h"
#include <cassert>
static int calls=0;
static void service(){
 ++calls;int v[NITROGEN_EXTRA_COUNT]={1,2,1,0,1};
 for(int i=0;i<NITROGEN_EXTRA_COUNT;++i){int w=nitrogen_extras_take(i);if(w>=0)v[i]=w;}
 nitrogen_extras_publish(1,v);
}
int main(){
 const int choices[]={3,3,2,2,2};
 for(int i=0;i<NITROGEN_EXTRA_COUNT;++i)assert(nitrogen_extras_choices(i)==choices[i]);
 assert(nitrogen_extras_choices(-1)==0&&nitrogen_extras_choices(NITROGEN_EXTRA_COUNT)==0);
 int v[NITROGEN_EXTRA_COUNT];
 assert(!nitrogen_extras_snapshot(v));               // nothing published yet
 // No verified game service registered: requests are dropped, never applied.
 nitrogen_extras_request(NITROGEN_EXTRA_LENS_TYPE,1);nitrogen_extras_service();
 assert(!nitrogen_extras_snapshot(v)&&nitrogen_extras_take(NITROGEN_EXTRA_LENS_TYPE)==-1);
 nitrogen_extras_set_service(service);
 nitrogen_extras_request(NITROGEN_EXTRA_LENS_FLARE,3);nitrogen_extras_request(NITROGEN_EXTRA_CAMERA_ROLL,-1);
 assert(nitrogen_extras_take(NITROGEN_EXTRA_LENS_FLARE)==-1&&nitrogen_extras_take(NITROGEN_EXTRA_CAMERA_ROLL)==-1);
 nitrogen_extras_service();assert(calls==1&&nitrogen_extras_snapshot(v)&&v[0]==1&&v[1]==2);
 nitrogen_extras_request(NITROGEN_EXTRA_HATCHING,1);nitrogen_extras_request(NITROGEN_EXTRA_HATCHING,0);
 nitrogen_extras_snapshot(v);assert(v[3]==0);         // latest request wins and is shown
 nitrogen_extras_service();assert(nitrogen_extras_take(NITROGEN_EXTRA_HATCHING)==-1);
 int bad[NITROGEN_EXTRA_COUNT]={9,9,9,9,9};nitrogen_extras_publish(0,bad);
 assert(!nitrogen_extras_snapshot(v));for(int x:v)assert(x==0);  // never shows garbage
}
""", ['window_display.cpp'])

    def test_runtime_serves_extras_between_frames_but_not_in_linked_play(self):
        host = (ROOT / 'compat' / 'runtime_host.cpp').read_text(encoding='utf-8')
        update = host[host.index('static void nitrogen_window_update()\n{'):]
        update = update[:update.index('\n}\n')]
        serve = update.index('if (sdl_window && !g_headless && !psx_netplay_active()) nitrogen_extras_service();')
        self.assertLess(serve, update.index('if (!g_gl_active'))   # software renderer too
        menu = (ROOT / 'compat' / 'desktop_menu.cpp').read_text(encoding='utf-8')
        extras = menu[menu.index('void extras_settings()'):]
        extras = extras[:extras.index('\n}\n')]
        for label in ('Lens flare', 'Lens type', 'Camera roll', 'Hatching order', 'Bonus weapons'):
            self.assertIn(f'"{label}"', extras)
        self.assertIn('nitrogen_extras_request(', extras)
        self.assertIn('BeginDisabled(!available)', extras)
        self.assertNotIn('g_psx_ram', menu)   # the menu never touches guest memory


@unittest.skipUnless(COMPILER, 'C++ compiler required for display fixtures')
class MenuBarTests(unittest.TestCase):
    def test_f1_reveals_and_hides_the_bar_with_and_without_autohide(self):
        run_fixture(self, r"""
#include "desktop_menu.h"
#include <cassert>
int main(){
 // Without autohide F1 toggles the saved preference, as before.
 NitrogenMenuBarState s{true,false,false};
 assert(nitrogen_menu_bar_shown(s,false,false));
 assert(nitrogen_menu_bar_f1(s,false,false)&&!s.visible&&!nitrogen_menu_bar_shown(s,false,false));
 assert(nitrogen_menu_bar_f1(s,false,false)&&s.visible&&nitrogen_menu_bar_shown(s,false,false));
 // Autohide, pointer away from the top (fullscreen hides the pointer):
 // F1 reveals the bar without changing the preference, F1 again re-hides it.
 s={true,true,false};
 assert(!nitrogen_menu_bar_shown(s,false,false));
 assert(!nitrogen_menu_bar_f1(s,false,false)&&s.peek&&nitrogen_menu_bar_shown(s,false,false));
 assert(!nitrogen_menu_bar_f1(s,false,false)&&!s.peek&&s.visible&&!nitrogen_menu_bar_shown(s,false,false));
 // Autohide with the bar switched off entirely: F1 still brings it back.
 s={false,true,false};
 assert(nitrogen_menu_bar_f1(s,false,false)&&s.visible&&s.peek&&nitrogen_menu_bar_shown(s,false,false));
 // Autohide, already shown by hovering: F1 leaves it to autohide again.
 s={true,true,false};
 assert(!nitrogen_menu_bar_f1(s,false,true)&&!s.peek&&s.visible);
 assert(nitrogen_menu_bar_shown(s,true,false));  // an open menu always shows it
}
""", [])

    def test_menu_uses_the_tested_policy_and_clears_peek(self):
        menu = (ROOT / 'compat' / 'desktop_menu.cpp').read_text(encoding='utf-8')
        self.assertIn('nitrogen_menu_bar_f1(s,', menu)
        self.assertIn('const bool bar = nitrogen_menu_bar_shown({m.visible, m.autohide, m.peek}', menu)
        focus = menu[menu.index('SDL_EVENT_WINDOW_FOCUS_LOST'):][:200]
        self.assertIn('m.peek = false;', focus)


@unittest.skipUnless(COMPILER, 'C++ compiler required for display fixtures')
class FramePacingTests(unittest.TestCase):
    def test_smoothest_refresh_is_an_even_multiple_of_the_game_rate(self):
        run_fixture(self, r"""
#include "window_display.h"
#include <cassert>
#include <cmath>
int main(){
 const double g=60000.0/1001.0;   // 59.94
 // The owner's 49-inch at 3840x1080: 144, 119.97, 99.96 and 59.97 Hz.
 const float c49[]={144.0f,119.97f,99.96f,59.97f};
 assert(nitrogen_window_smooth_refresh(c49,4,g)==119.97f);
 const float only144[]={144.0f}; assert(nitrogen_window_smooth_refresh(only144,1,g)==0);
 const float plain[]={60.0f,75.0f}; assert(nitrogen_window_smooth_refresh(plain,2,g)==60.0f);
 const float fast[]={240.0f,165.0f,120.0f}; assert(nitrogen_window_smooth_refresh(fast,3,g)==240.0f);
 const float none[]={100.0f,50.0f,72.0f}; assert(nitrogen_window_smooth_refresh(none,3,g)==0);
 assert(nitrogen_window_smooth_refresh(nullptr,0,g)==0);
 int even=-1;
 assert(std::fabs(nitrogen_window_refreshes_per_frame(144,g,&even)-2.4024)<.001 && !even);
 assert(std::fabs(nitrogen_window_refreshes_per_frame(119.97,g,&even)-2.0015)<.001 && even);
 nitrogen_window_refreshes_per_frame(59.97,g,&even);assert(even);
 nitrogen_window_refreshes_per_frame(75,g,&even);assert(!even);
 assert(nitrogen_window_refreshes_per_frame(0,g,&even)==0 && !even);
 nitrogen_window_report_refresh(1,119.97f,0);int ch,ov;float hz;nitrogen_window_refresh_report(&ch,&hz,&ov);
 assert(ch==1&&hz==119.97f&&!ov);
}
""", ['window_display.cpp'])

    def test_fullscreen_refresh_persists_and_reaches_the_framework_helper(self):
        run_fixture(self, r"""
#include "nitrogen_options.h"
#include <cassert>
int main(int,char**argv){
 nitrogen_options_init(argv[1]);
 assert(nitrogen_option_get(NITROGEN_FULLSCREEN_REFRESH)==NITROGEN_REFRESH_DESKTOP);  // default
 assert(!nitrogen_option_set(NITROGEN_FULLSCREEN_REFRESH,2));
 assert(nitrogen_option_set(NITROGEN_FULLSCREEN_REFRESH,NITROGEN_REFRESH_SMOOTH_60));
}
""", ['game_options.cpp'], args=('unused/',), env=dict(os.environ, NITROGEN_OPTIONS_FILE=os.devnull))
        host = (ROOT / 'compat' / 'runtime_host.cpp').read_text(encoding='utf-8')
        update = host[host.index('static void nitrogen_window_update()\n{'):]
        update = update[:update.index('\n}\n')]
        # The choice is set before the video mode is applied, so entering
        # Fullscreen already uses it; a developer override is respected.
        self.assertLess(update.index('nitrogen_exclusive_refresh_env(hz);'),
                        update.index('psx_window_fullscreen_set(sdl_window'))
        self.assertIn('getenv("PSX_EXCLUSIVE_REFRESH_HZ")', update)
        self.assertIn('_putenv_s("PSX_EXCLUSIVE_REFRESH_HZ"', host)


class SettingsParityTests(unittest.TestCase):
    """V1.2: the in-game Video menu and F1 Settings offer the same six options."""
    LABELS = {'NITROGEN_VIDEO_MODE': 'Window mode', 'NITROGEN_HUD_SCALE': 'HUD scale',
              'NITROGEN_FULLSCREEN_REFRESH': 'Fullscreen refresh',
              'NITROGEN_RENDER_PERCENT': 'Render resolution', 'NITROGEN_HUD_LAYOUT': 'HUD position',
              'NITROGEN_ASPECT': 'Aspect', 'NITROGEN_VSYNC': 'VSync'}

    def test_f1_controls_set_the_shared_options(self):
        menu = (ROOT / 'compat' / 'desktop_menu.cpp').read_text(encoding='utf-8')
        for key, label in self.LABELS.items():
            with self.subTest(key=key):
                at = menu.index(f'"{label}"')
                self.assertIn(f'nitrogen_option_set({key}', menu[at:at + 400])

    def test_video_menu_rows_cover_the_same_options(self):
        entry = (ROOT / 'native' / 'options_entry.cpp').read_text(encoding='utf-8')
        rows = entry[entry.index('constexpr int keys[]{'):]
        rows = rows[:rows.index('};')]
        self.assertEqual([k.strip() for k in rows.split('{', 1)[1].split(',')],
                         ['NITROGEN_VIDEO_MODE', 'NITROGEN_FULLSCREEN_REFRESH', 'NITROGEN_HUD_SCALE', 'NITROGEN_HUD_LAYOUT',
                          'NITROGEN_ASPECT', 'NITROGEN_VSYNC', 'NITROGEN_RENDER_PERCENT'])
        self.assertEqual(set(self.LABELS), {k.strip() for k in rows.split('{', 1)[1].split(',')})


if __name__ == '__main__':
    unittest.main()
