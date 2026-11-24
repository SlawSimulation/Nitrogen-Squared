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
 if(argc>4&&!std::strcmp(argv[4],"write")){
  assert(!nitrogen_option_set(NITROGEN_ASPECT,5));
  assert(!nitrogen_option_set(NITROGEN_ASPECT,-1));
  assert(!nitrogen_option_set(NITROGEN_VSYNC,2));
  assert(nitrogen_option_set(NITROGEN_ASPECT,NITROGEN_ASPECT_21_9));
  assert(nitrogen_option_set(NITROGEN_VSYNC,0));
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
            run_fixture(self, source, ['game_options.cpp'], [temp + '/', '3', '0'], env)
            # Invalid stored values are ignored, never applied.
            config.write_text('aspect 7\nvsync 2\n')
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


if __name__ == '__main__':
    unittest.main()
