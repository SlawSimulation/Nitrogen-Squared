"""GPL-3.0-or-later. Read-only Windows display inventory for measurement reports.

Lists what Windows exposes for each attached display: the current mode, every
mode EnumDisplaySettings offers, and, through QueryDisplayConfig, the monitor's
EDID name and its preferred (native) mode. Modes larger than the preferred mode
are flagged: they are usually GPU-scaled (for example NVIDIA DSR) rather than
native panel modes. Nothing is changed. Other platforms return an explanation.
"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes


def inventory() -> dict:
    if os.name != "nt":
        return dict(available=False, reason="Windows display inventory is only available on Windows")
    try:
        return _inventory()
    except (OSError, AttributeError, ValueError) as e:
        return dict(available=False, reason=f"Windows display query failed: {e}")


class DEVMODEW(ctypes.Structure):
    _fields_ = [("dmDeviceName", wintypes.WCHAR * 32), ("dmSpecVersion", wintypes.WORD),
                ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD),
                ("dmFields", wintypes.DWORD), ("dmPositionX", wintypes.LONG), ("dmPositionY", wintypes.LONG),
                ("dmDisplayOrientation", wintypes.DWORD), ("dmDisplayFixedOutput", wintypes.DWORD),
                ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short), ("dmYResolution", ctypes.c_short),
                ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short), ("dmFormName", wintypes.WCHAR * 32),
                ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD), ("dmPelsWidth", wintypes.DWORD),
                ("dmPelsHeight", wintypes.DWORD), ("dmDisplayFlags", wintypes.DWORD),
                ("dmDisplayFrequency", wintypes.DWORD), ("dmICMMethod", wintypes.DWORD),
                ("dmICMIntent", wintypes.DWORD), ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD),
                ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
                ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD)]


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32), ("DeviceString", wintypes.WCHAR * 128),
                ("StateFlags", wintypes.DWORD), ("DeviceID", wintypes.WCHAR * 128),
                ("DeviceKey", wintypes.WCHAR * 128)]


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", ctypes.c_uint32), ("Denominator", ctypes.c_uint32)]


class PATH_SOURCE(ctypes.Structure):
    _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                ("statusFlags", ctypes.c_uint32)]


class PATH_TARGET(ctypes.Structure):
    _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                ("outputTechnology", ctypes.c_uint32), ("rotation", ctypes.c_uint32), ("scaling", ctypes.c_uint32),
                ("refreshRate", RATIONAL), ("scanLineOrdering", ctypes.c_uint32), ("targetAvailable", wintypes.BOOL),
                ("statusFlags", ctypes.c_uint32)]


class PATH_INFO(ctypes.Structure):
    _fields_ = [("sourceInfo", PATH_SOURCE), ("targetInfo", PATH_TARGET), ("flags", ctypes.c_uint32)]


class MODE_INFO(ctypes.Structure):
    _fields_ = [("infoType", ctypes.c_uint32), ("id", ctypes.c_uint32), ("adapterId", LUID),
                ("union", ctypes.c_byte * 48)]


class HEADER(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32), ("adapterId", LUID), ("id", ctypes.c_uint32)]


class SOURCE_NAME(ctypes.Structure):
    _fields_ = [("header", HEADER), ("viewGdiDeviceName", wintypes.WCHAR * 32)]


class TARGET_NAME(ctypes.Structure):
    _fields_ = [("header", HEADER), ("flags", ctypes.c_uint32), ("outputTechnology", ctypes.c_uint32),
                ("edidManufactureId", ctypes.c_uint16), ("edidProductCodeId", ctypes.c_uint16),
                ("connectorInstance", ctypes.c_uint32), ("monitorFriendlyDeviceName", wintypes.WCHAR * 64),
                ("monitorDevicePath", wintypes.WCHAR * 128)]


class REGION(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_uint32), ("cy", ctypes.c_uint32)]


class SIGNAL(ctypes.Structure):
    _fields_ = [("pixelRate", ctypes.c_uint64), ("hSyncFreq", RATIONAL), ("vSyncFreq", RATIONAL),
                ("activeSize", REGION), ("totalSize", REGION), ("videoStandard", ctypes.c_uint32),
                ("scanLineOrdering", ctypes.c_uint32)]


class PREFERRED_MODE(ctypes.Structure):
    _fields_ = [("header", HEADER), ("width", ctypes.c_uint32), ("height", ctypes.c_uint32),
                ("targetMode", SIGNAL)]


GET_SOURCE_NAME, GET_TARGET_NAME, GET_TARGET_PREFERRED_MODE = 1, 2, 3
QDC_ONLY_ACTIVE_PATHS = 2
ENUM_CURRENT_SETTINGS = 0xFFFFFFFF
DISPLAY_DEVICE_ATTACHED_TO_DESKTOP, DISPLAY_DEVICE_PRIMARY_DEVICE = 0x1, 0x4


def _rate(r: RATIONAL) -> float | None:
    return round(r.Numerator / r.Denominator, 3) if r.Denominator else None


def _config_targets(user32) -> dict:
    """GDI device name -> monitor name and preferred mode, from QueryDisplayConfig."""
    paths, modes = ctypes.c_uint32(0), ctypes.c_uint32(0)
    if user32.GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(paths), ctypes.byref(modes)):
        return {}
    path_array = (PATH_INFO * paths.value)()
    mode_array = (MODE_INFO * modes.value)()
    if user32.QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(paths), path_array, ctypes.byref(modes),
                                 mode_array, None):
        return {}
    result = {}
    for path in path_array[:paths.value]:
        source = SOURCE_NAME()
        source.header = HEADER(GET_SOURCE_NAME, ctypes.sizeof(SOURCE_NAME), path.sourceInfo.adapterId,
                               path.sourceInfo.id)
        if user32.DisplayConfigGetDeviceInfo(ctypes.byref(source)):
            continue
        target = TARGET_NAME()
        target.header = HEADER(GET_TARGET_NAME, ctypes.sizeof(TARGET_NAME), path.targetInfo.adapterId,
                               path.targetInfo.id)
        name = target.monitorFriendlyDeviceName if not user32.DisplayConfigGetDeviceInfo(ctypes.byref(target)) else ""
        preferred = PREFERRED_MODE()
        preferred.header = HEADER(GET_TARGET_PREFERRED_MODE, ctypes.sizeof(PREFERRED_MODE),
                                  path.targetInfo.adapterId, path.targetInfo.id)
        pref = None
        if not user32.DisplayConfigGetDeviceInfo(ctypes.byref(preferred)):
            pref = dict(w=preferred.width, h=preferred.height, refresh=_rate(preferred.targetMode.vSyncFreq))
        result[source.viewGdiDeviceName] = dict(monitor_name=name or None, preferred_mode=pref,
                                                path_refresh=_rate(path.targetInfo.refreshRate))
    return result


def _inventory() -> dict:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    targets = _config_targets(user32)
    displays = []
    index = 0
    while True:
        device = DISPLAY_DEVICEW(); device.cb = ctypes.sizeof(DISPLAY_DEVICEW)
        if not user32.EnumDisplayDevicesW(None, index, ctypes.byref(device), 0):
            break
        index += 1
        if not device.StateFlags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP:
            continue
        current = DEVMODEW(); current.dmSize = ctypes.sizeof(DEVMODEW)
        user32.EnumDisplaySettingsW(device.DeviceName, ENUM_CURRENT_SETTINGS, ctypes.byref(current))
        modes, n = set(), 0
        while True:
            mode = DEVMODEW(); mode.dmSize = ctypes.sizeof(DEVMODEW)
            if not user32.EnumDisplaySettingsW(device.DeviceName, n, ctypes.byref(mode)):
                break
            n += 1
            if mode.dmBitsPerPel >= 24:
                modes.add((mode.dmPelsWidth, mode.dmPelsHeight, mode.dmDisplayFrequency))
        info = targets.get(device.DeviceName, {})
        pref = info.get("preferred_mode")
        listed = [dict(w=w, h=h, refresh=r, above_native=bool(pref and (w > pref["w"] or h > pref["h"])))
                  for w, h, r in sorted(modes, reverse=True)]
        displays.append(dict(
            gdi_name=device.DeviceName, adapter=device.DeviceString,
            primary=bool(device.StateFlags & DISPLAY_DEVICE_PRIMARY_DEVICE),
            monitor_name=info.get("monitor_name"), preferred_native_mode=pref,
            current=dict(w=current.dmPelsWidth, h=current.dmPelsHeight, refresh=current.dmDisplayFrequency,
                         bits=current.dmBitsPerPel, position=[current.dmPositionX, current.dmPositionY],
                         precise_refresh=info.get("path_refresh")),
            mode_count=len(listed), modes=listed))
    return dict(available=True, windows=sys.getwindowsversion().build if hasattr(sys, "getwindowsversion") else None,
                displays=displays,
                note="above_native marks modes larger than the monitor's preferred mode; they are usually "
                     "GPU-scaled (for example NVIDIA DSR), not native panel resolutions.")


if __name__ == "__main__":
    import json
    print(json.dumps(inventory(), indent=2))
