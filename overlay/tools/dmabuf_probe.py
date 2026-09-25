#!/usr/bin/env python3
"""
dmabuf_probe.py - show the dmabuf formats/modifiers SteamVR's IPC resource manager offers.

gamescope's OpenVR backend asks for exactly this at startup and (with --allow-deferred-backend, as on
the Steam Frame) aborts with `Assertion !modifiers.empty()` when the answer is empty. Run this on the
headset with and without other overlay applications connected to see what changes:

    python3 overlay/tools/dmabuf_probe.py [--app-type background|overlay]
"""
import argparse
import ctypes
import os
import sys
from ctypes import POINTER, byref, c_bool, c_int, c_uint32, c_uint64

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import openvr_min as ovr  # noqa: E402

RESOURCE_MANAGER_VERSIONS = ("IVRIPCResourceManagerClient_003",)
RESOURCE_MANAGER_COUNT = 10


def fourcc(code):
    return bytes((code & 0xFF, (code >> 8) & 0xFF, (code >> 16) & 0xFF, (code >> 24) & 0xFF)).decode("latin-1")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--app-type", choices=("background", "overlay"), default="background")
    parser.add_argument("--query-as", choices=("background", "overlay"), default="overlay",
                        help="application type passed to GetDmabufModifiers (gamescope passes overlay)")
    args = parser.parse_args()
    types = {"background": ovr.VRApplication_Background, "overlay": ovr.VRApplication_Overlay}

    session = ovr.Session().init(types[args.app_type])
    try:
        table, version = ovr.get_interface(session.lib, RESOURCE_MANAGER_VERSIONS, RESOURCE_MANAGER_COUNT)
        get_formats = table.fn(5, c_bool, POINTER(c_uint32), POINTER(c_uint32))
        get_modifiers = table.fn(6, c_bool, c_int, c_uint32, POINTER(c_uint32), POINTER(c_uint64))

        count = c_uint32(0)
        ok = get_formats(byref(count), None)
        print("%s: GetDmabufFormats -> ok=%s count=%d" % (version, ok, count.value))
        formats = (c_uint32 * max(1, count.value))()
        if count.value:
            get_formats(byref(count), formats)
        total = 0
        for i in range(count.value):
            fmt = formats[i]
            mods = c_uint32(0)
            ok = get_modifiers(types[args.query_as], fmt, byref(mods), None)
            values = (c_uint64 * max(1, mods.value))()
            if mods.value:
                get_modifiers(types[args.query_as], fmt, byref(mods), values)
            total += mods.value
            shown = ", ".join("0x%016x" % values[j] for j in range(min(mods.value, 4)))
            print("  %s (0x%08x): ok=%s modifiers=%d %s%s" % (fourcc(fmt), fmt, ok, mods.value, shown,
                                                            " ..." if mods.value > 4 else ""))
        print("summary: %d formats, %d modifiers in total%s" % (count.value, total,
                                                                " -> gamescope would ASSERT" if count.value and total == 0 or not count.value else ""))
        return 0 if total else 1
    finally:
        session.shutdown()


if __name__ == "__main__":
    sys.exit(main())
