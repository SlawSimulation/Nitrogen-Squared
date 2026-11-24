#pragma once

#ifdef __cplusplus
extern "C" {
#endif
void nitrogen_menu_letterbox(int ww, int wh, int num, int den,
                            int *x, int *y, int *w, int *h);
int nitrogen_menu_reserved_height(void);
#ifdef __cplusplus
}

// Menu-bar visibility. With autohide the bar appears while the pointer is at
// the top or a menu is open; "peek" is F1's explicit reveal, so the bar can be
// brought back without aiming the pointer (fullscreen hides the pointer).
struct NitrogenMenuBarState { bool visible, autohide, peek; };
inline bool nitrogen_menu_bar_shown(const NitrogenMenuBarState &s, bool open, bool over_bar)
{
    return s.visible && (!s.autohide || s.peek || open || over_bar);
}
// F1: show the bar if it is hidden for any reason, otherwise hide it.
// Returns true when the saved "show menu bar" preference changed.
inline bool nitrogen_menu_bar_f1(NitrogenMenuBarState &s, bool open, bool over_bar)
{
    const bool was_visible = s.visible;
    if (!nitrogen_menu_bar_shown(s, open, over_bar)) {
        s.visible = true;
        s.peek = s.autohide;
    } else if (s.autohide) {
        s.peek = false;          // back to autohide; the preference is kept
    } else {
        s.visible = false;
    }
    return s.visible != was_visible;
}
#endif
