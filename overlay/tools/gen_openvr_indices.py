"""Prints the function-table positions used by openvr_min.py from an OpenVR SDK openvr_capi.h,
so they can be re-checked after an SDK update.

    python steamframe/overlay/tools/gen_openvr_indices.py path/to/openvr_capi.h
"""
import re
import sys

WANTED = {
    "IVRSystem": ["PollNextEvent", "AcknowledgeQuit_Exiting", "GetRuntimeVersion"],
    "IVROverlay": ["FindOverlay", "CreateOverlay", "DestroyOverlay", "GetOverlayErrorNameFromEnum", "SetOverlayFlag",
                   "SetOverlayColor", "SetOverlayAlpha", "SetOverlaySortOrder", "SetOverlayWidthInMeters",
                   "SetOverlayTextureBounds", "ShowOverlay", "HideOverlay", "IsOverlayVisible",
                   "PollNextOverlayEvent", "SetOverlayInputMethod", "SetOverlayMouseScale", "SetOverlayTexture",
                   "ClearOverlayTexture", "SetOverlayRaw", "SetOverlayFromFile", "GetOverlayTextureSize",
                   "CreateDashboardOverlay", "IsDashboardVisible", "IsActiveDashboardOverlay", "ShowDashboard",
                   "ShowKeyboardForOverlay", "GetKeyboardText", "HideKeyboard"],
    "IVRApplications": ["AddApplicationManifest", "RemoveApplicationManifest", "IsApplicationInstalled",
                        "LaunchApplication", "LaunchDashboardOverlay", "IdentifyApplication",
                        "GetApplicationProcessId", "GetApplicationsErrorNameFromEnum", "SetApplicationAutoLaunch",
                        "GetApplicationAutoLaunch"],
}

source = open(sys.argv[1], encoding="utf-8", errors="replace").read()
for interface, names in WANTED.items():
    body = re.search(r"struct VR_%s_FnTable\s*\{(.*?)\n\};" % interface, source, re.S).group(1)
    table = re.findall(r"\*\s*(\w+)\)\s*\(", body)
    version = re.search(r'static const char \* %s_Version = "(\w+)";' % interface, source).group(1)
    print("%s (%s): %d entries" % (interface, version, len(table)))
    for name in names:
        print("    %s = %d" % (name, table.index(name)))
