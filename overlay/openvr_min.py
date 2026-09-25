"""
Minimal ctypes binding to the OpenVR runtime (libopenvr_api.so) for the FCAM overlay.

Only the calls the overlay needs are exposed, through the C "FnTable:" interface tables.
Function positions come from openvr_capi.h of OpenVR SDK 2.15.6 (see tools/gen_openvr_indices.py);
the Steam Frame runtime (SteamVR build 1789606310, Sep 2026) accepts these interface versions.
Standard library only; no pyopenvr.
"""

import ctypes
import json
import os
import struct
from ctypes import POINTER, byref, c_bool, c_char_p, c_float, c_int, c_ssize_t, c_uint32, c_uint64, c_void_p

# --- constants (openvr_capi.h) ------------------------------------------------------------------

VRApplication_Overlay = 2
VRApplication_Background = 3

VROverlayInputMethod_None = 0
VROverlayInputMethod_Mouse = 1

VROverlayFlags_VisibleInDashboard = 32768
VROverlayFlags_MakeOverlaysInteractiveIfVisible = 65536

VREvent_MouseMove = 300
VREvent_MouseButtonDown = 301
VREvent_MouseButtonUp = 302
VREvent_OverlayShown = 500
VREvent_OverlayHidden = 501
VREvent_DashboardActivated = 502
VREvent_DashboardDeactivated = 503
VREvent_Quit = 700
VREvent_ProcessQuit = 701
VREvent_RestartRequested = 705

VRInitError_None = 0
VRInitError_Init_InstallationNotFound = 100
VRInitError_Init_NoServerForBackgroundApp = 121

# Interface versions to try, newest first. The runtime keeps older versions callable and new
# functions are appended to the tables, so an older table layout works with a newer version.
IVRSystem_Versions = ("IVRSystem_026", "IVRSystem_025", "IVRSystem_024", "IVRSystem_023", "IVRSystem_022")
IVROverlay_Versions = ("IVROverlay_028", "IVROverlay_029", "IVROverlay_027", "IVROverlay_026", "IVROverlay_025")
IVRApplications_Versions = ("IVRApplications_008", "IVRApplications_007")

IVRSystem_Count = 51
IVROverlay_Count = 82
IVRApplications_Count = 31

LIBRARY_CANDIDATES = (
    "/opt/steamvr/bin/linuxarm64/libopenvr_api.so",
    "/usr/lib/libopenvr_api.so",
    "/opt/steamvr/bin/linux64/libopenvr_api.so",
    "libopenvr_api.so",
)


class OpenVRError(Exception):
    pass


class VREvent_t(ctypes.Structure):
    """VREvent_t as laid out on Linux (4-byte packed, 48-byte data union)."""
    _pack_ = 4
    _fields_ = [
        ("eventType", c_uint32),
        ("trackedDeviceIndex", c_uint32),
        ("eventAgeSeconds", c_float),
        ("data", ctypes.c_uint8 * 48),
    ]

    def mouse(self):
        """(x, y, button) of a VREvent_Mouse_t payload."""
        return struct.unpack_from("<ffI", bytes(self.data))


class HmdVector2_t(ctypes.Structure):
    _fields_ = [("v", c_float * 2)]


assert ctypes.sizeof(VREvent_t) == 60


# --- library --------------------------------------------------------------------------------------

def runtime_dirs():
    """Runtime directories from ~/.config/openvr/openvrpaths.vrpath, if present."""
    path = os.path.expanduser("~/.config/openvr/openvrpaths.vrpath")
    try:
        with open(path, encoding="utf-8") as fh:
            return list(json.load(fh).get("runtime", []))
    except (OSError, ValueError):
        return []


def find_library(explicit=None):
    candidates = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("FCAM_OPENVR_LIB")
    if env:
        candidates.append(env)
    for runtime in runtime_dirs():
        for arch in ("linuxarm64", "linux64"):
            candidates.append(os.path.join(runtime, "bin", arch, "libopenvr_api.so"))
    candidates.extend(LIBRARY_CANDIDATES)
    for candidate in candidates:
        if os.path.isabs(candidate) and not os.path.exists(candidate):
            continue
        try:
            return ctypes.CDLL(candidate), candidate
        except OSError:
            continue
    raise OpenVRError("libopenvr_api.so not found (tried: %s)" % ", ".join(candidates))


def _prototype(lib):
    lib.VR_InitInternal2.restype = c_ssize_t
    lib.VR_InitInternal2.argtypes = [POINTER(c_int), c_int, c_char_p]
    lib.VR_ShutdownInternal.restype = None
    lib.VR_ShutdownInternal.argtypes = []
    lib.VR_IsInterfaceVersionValid.restype = c_bool
    lib.VR_IsInterfaceVersionValid.argtypes = [c_char_p]
    lib.VR_GetGenericInterface.restype = c_ssize_t
    lib.VR_GetGenericInterface.argtypes = [c_char_p, POINTER(c_int)]
    lib.VR_GetVRInitErrorAsEnglishDescription.restype = c_char_p
    lib.VR_GetVRInitErrorAsEnglishDescription.argtypes = [c_int]
    lib.VR_GetVRInitErrorAsSymbol.restype = c_char_p
    lib.VR_GetVRInitErrorAsSymbol.argtypes = [c_int]
    lib.VR_IsRuntimeInstalled.restype = c_bool
    lib.VR_IsRuntimeInstalled.argtypes = []


class FnTable:
    """A C function table returned by VR_GetGenericInterface("FnTable:...")."""

    def __init__(self, address, count):
        self._entries = ctypes.cast(address, POINTER(c_void_p * count)).contents

    def fn(self, index, restype, *argtypes):
        address = self._entries[index]
        if not address:
            raise OpenVRError("function %d missing from interface table" % index)
        return ctypes.CFUNCTYPE(restype, *argtypes)(address)


def get_interface(lib, versions, count):
    for version in versions:
        if not lib.VR_IsInterfaceVersionValid(version.encode()):
            continue
        err = c_int(0)
        address = lib.VR_GetGenericInterface(b"FnTable:" + version.encode(), byref(err))
        if address and err.value == VRInitError_None:
            return FnTable(address, count), version
    raise OpenVRError("runtime supports none of %s" % ", ".join(versions))


# --- interfaces -----------------------------------------------------------------------------------

class System:
    def __init__(self, lib):
        table, self.version = get_interface(lib, IVRSystem_Versions, IVRSystem_Count)
        self._poll_next_event = table.fn(30, c_bool, POINTER(VREvent_t), c_uint32)
        self._acknowledge_quit_exiting = table.fn(47, None)
        self._get_runtime_version = table.fn(49, c_char_p)

    def poll_next_event(self):
        event = VREvent_t()
        if self._poll_next_event(byref(event), ctypes.sizeof(event)):
            return event
        return None

    def acknowledge_quit_exiting(self):
        self._acknowledge_quit_exiting()

    def runtime_version(self):
        value = self._get_runtime_version()
        return value.decode(errors="replace") if value else ""


class Overlay:
    def __init__(self, lib):
        table, self.version = get_interface(lib, IVROverlay_Versions, IVROverlay_Count)
        self._find = table.fn(0, c_int, c_char_p, POINTER(c_uint64))
        self._create = table.fn(1, c_int, c_char_p, c_char_p, POINTER(c_uint64))
        self._destroy = table.fn(3, c_int, c_uint64)
        self._error_name = table.fn(8, c_char_p, c_int)
        self._set_flag = table.fn(11, c_int, c_uint64, c_int, c_bool)
        self._set_color = table.fn(14, c_int, c_uint64, c_float, c_float, c_float)
        self._set_alpha = table.fn(16, c_int, c_uint64, c_float)
        self._set_width = table.fn(22, c_int, c_uint64, c_float)
        self._show = table.fn(43, c_int, c_uint64)
        self._hide = table.fn(44, c_int, c_uint64)
        self._is_visible = table.fn(45, c_bool, c_uint64)
        self._poll_event = table.fn(48, c_bool, c_uint64, POINTER(VREvent_t), c_uint32)
        self._set_input_method = table.fn(50, c_int, c_uint64, c_int)
        self._set_mouse_scale = table.fn(52, c_int, c_uint64, POINTER(HmdVector2_t))
        self._set_raw = table.fn(62, c_int, c_uint64, c_void_p, c_uint32, c_uint32, c_uint32)
        self._set_from_file = table.fn(63, c_int, c_uint64, c_char_p)
        self._create_dashboard = table.fn(67, c_int, c_char_p, c_char_p, POINTER(c_uint64), POINTER(c_uint64))
        self._is_dashboard_visible = table.fn(68, c_bool)
        self._is_active_dashboard_overlay = table.fn(69, c_bool, c_uint64)
        self._show_dashboard = table.fn(72, None, c_char_p)

    def error_name(self, code):
        value = self._error_name(code)
        return value.decode(errors="replace") if value else str(code)

    def _check(self, code, what):
        if code != 0:
            raise OpenVRError("%s failed: %s (%d)" % (what, self.error_name(code), code))

    def find_overlay(self, key):
        handle = c_uint64(0)
        return handle.value if self._find(key.encode(), byref(handle)) == 0 else None

    def create_overlay(self, key, name):
        handle = c_uint64(0)
        self._check(self._create(key.encode(), name.encode(), byref(handle)), "CreateOverlay")
        return handle.value

    def create_dashboard_overlay(self, key, name):
        main, thumb = c_uint64(0), c_uint64(0)
        self._check(self._create_dashboard(key.encode(), name.encode(), byref(main), byref(thumb)),
                    "CreateDashboardOverlay")
        return main.value, thumb.value

    def destroy_overlay(self, handle):
        self._destroy(handle)

    def set_flag(self, handle, flag, enabled):
        self._check(self._set_flag(handle, flag, enabled), "SetOverlayFlag")

    def set_color(self, handle, r, g, b):
        self._check(self._set_color(handle, r, g, b), "SetOverlayColor")

    def set_alpha(self, handle, alpha):
        self._check(self._set_alpha(handle, alpha), "SetOverlayAlpha")

    def set_width_meters(self, handle, width):
        self._check(self._set_width(handle, width), "SetOverlayWidthInMeters")

    def show(self, handle):
        self._check(self._show(handle), "ShowOverlay")

    def hide(self, handle):
        self._check(self._hide(handle), "HideOverlay")

    def is_visible(self, handle):
        return bool(self._is_visible(handle))

    def poll_next_overlay_event(self, handle):
        event = VREvent_t()
        if self._poll_event(handle, byref(event), ctypes.sizeof(event)):
            return event
        return None

    def set_input_method(self, handle, method):
        self._check(self._set_input_method(handle, method), "SetOverlayInputMethod")

    def set_mouse_scale(self, handle, width, height):
        scale = HmdVector2_t()
        scale.v[0], scale.v[1] = float(width), float(height)
        self._check(self._set_mouse_scale(handle, byref(scale)), "SetOverlayMouseScale")

    def set_raw(self, handle, pixels, width, height, bytes_per_pixel=4):
        """Uploads an RGBA8 image from a bytearray (or any writable buffer) of width*height*4 bytes."""
        expected = width * height * bytes_per_pixel
        if len(pixels) != expected:
            raise ValueError("pixel buffer is %d bytes, expected %d" % (len(pixels), expected))
        array = (ctypes.c_ubyte * expected).from_buffer(pixels)
        self._check(self._set_raw(handle, array, width, height, bytes_per_pixel), "SetOverlayRaw")

    def set_from_file(self, handle, path):
        self._check(self._set_from_file(handle, os.fsencode(path)), "SetOverlayFromFile")

    def is_dashboard_visible(self):
        return bool(self._is_dashboard_visible())

    def is_active_dashboard_overlay(self, handle):
        return bool(self._is_active_dashboard_overlay(handle))

    def show_dashboard(self, key):
        self._show_dashboard(key.encode())


class Applications:
    def __init__(self, lib):
        table, self.version = get_interface(lib, IVRApplications_Versions, IVRApplications_Count)
        self._add_manifest = table.fn(0, c_int, c_char_p, c_bool)
        self._remove_manifest = table.fn(1, c_int, c_char_p)
        self._is_installed = table.fn(2, c_bool, c_char_p)
        self._launch = table.fn(6, c_int, c_char_p)
        self._launch_dashboard_overlay = table.fn(9, c_int, c_char_p)
        self._identify = table.fn(11, c_int, c_uint32, c_char_p)
        self._process_id = table.fn(12, c_uint32, c_char_p)
        self._error_name = table.fn(13, c_char_p, c_int)
        self._set_auto_launch = table.fn(17, c_int, c_char_p, c_bool)
        self._get_auto_launch = table.fn(18, c_bool, c_char_p)

    def error_name(self, code):
        value = self._error_name(code)
        return value.decode(errors="replace") if value else str(code)

    def _check(self, code, what):
        if code != 0:
            raise OpenVRError("%s failed: %s (%d)" % (what, self.error_name(code), code))

    def add_manifest(self, path, temporary=False):
        self._check(self._add_manifest(os.fsencode(os.path.abspath(path)), temporary), "AddApplicationManifest")

    def remove_manifest(self, path):
        self._check(self._remove_manifest(os.fsencode(os.path.abspath(path))), "RemoveApplicationManifest")

    def is_installed(self, key):
        return bool(self._is_installed(key.encode()))

    def launch(self, key):
        self._check(self._launch(key.encode()), "LaunchApplication")

    def launch_dashboard_overlay(self, key):
        self._check(self._launch_dashboard_overlay(key.encode()), "LaunchDashboardOverlay")

    def identify(self, pid, key):
        self._check(self._identify(pid, key.encode()), "IdentifyApplication")

    def process_id(self, key):
        return int(self._process_id(key.encode()))

    def set_auto_launch(self, key, enabled):
        self._check(self._set_auto_launch(key.encode(), enabled), "SetApplicationAutoLaunch")

    def get_auto_launch(self, key):
        return bool(self._get_auto_launch(key.encode()))


# --- session --------------------------------------------------------------------------------------

class Session:
    """One VR_Init/VR_Shutdown session. Use init() then the system/overlay/applications attributes."""

    def __init__(self, library=None):
        self.lib, self.library_path = find_library(library)
        _prototype(self.lib)
        self.token = None
        self.system = None
        self.overlay = None
        self.applications = None

    def error_text(self, code):
        symbol = self.lib.VR_GetVRInitErrorAsSymbol(code)
        description = self.lib.VR_GetVRInitErrorAsEnglishDescription(code)
        return "%s: %s" % (symbol.decode() if symbol else code, description.decode() if description else "")

    def init(self, app_type=VRApplication_Overlay):
        err = c_int(0)
        token = self.lib.VR_InitInternal2(byref(err), app_type, None)
        if err.value != VRInitError_None:
            raise OpenVRError("VR_Init failed (%d) %s" % (err.value, self.error_text(err.value)))
        self.token = token
        self.system = System(self.lib)
        self.overlay = Overlay(self.lib)
        self.applications = Applications(self.lib)
        return self

    def shutdown(self):
        if self.token is not None:
            self.lib.VR_ShutdownInternal()
            self.token = None
        self.system = self.overlay = self.applications = None
