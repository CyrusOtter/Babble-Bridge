"""
gl_texture.py - a persistent OpenGL ES texture for streaming an RGBA raster into a SteamVR overlay.

IVROverlay::SetOverlayRaw makes the compositor recreate the overlay texture on every call, which
shows a blank frame each time (visible flicker when updating more than about once a second), and on
the Steam Frame it leaves vrcompositor holding a client-owned buffer that crashes it when the client
exits. SetOverlayTexture with an OpenGL texture keeps one texture alive and only its contents change.

This uses EGL's surfaceless platform (Mesa) with a GLES context, so it works headless and without a
window system; on the Steam Frame that is Mesa zink on Turnip. Standard library only (ctypes).
Importing this module loads no library; libEGL/libGLESv2 are opened by GlTexture().

Thread affinity: the context is made current on the thread that creates the object and stays
current there. upload(), finish(), close() and the SetOverlayTexture call must run on that thread;
the first three raise GlError when called from another one.

Lifetime: keep one GlTexture for the whole process, also across VR_Shutdown/VR_Init. close() does
not call eglTerminate unless asked: SteamVR's client library works on the same EGL display inside
this process.

Shared by the FCAM overlay (Babble-Bridge/overlay) and the Steam Frame Eye overlay
(SteamFrameEyeModule/headset); keep both copies identical.
"""
import ctypes
import logging
import threading
from ctypes import POINTER, byref, c_char_p, c_int, c_uint, c_void_p

EGL_PLATFORM_SURFACELESS_MESA = 0x31DD
EGL_PLATFORM_GBM_MESA = 0x31D7
EGL_EXTENSIONS = 0x3055
EGL_VENDOR = 0x3053
EGL_VERSION = 0x3054
EGL_OPENGL_ES_API = 0x30A0
EGL_RENDERABLE_TYPE = 0x3040
EGL_OPENGL_ES2_BIT = 0x0004
EGL_SURFACE_TYPE = 0x3033
EGL_PBUFFER_BIT = 0x0001
EGL_RED_SIZE, EGL_GREEN_SIZE, EGL_BLUE_SIZE, EGL_ALPHA_SIZE = 0x3024, 0x3023, 0x3022, 0x3021
EGL_NONE = 0x3038
EGL_CONTEXT_CLIENT_VERSION = 0x3098
EGL_SUCCESS = 0x3000
EGL_FALSE = 0

GL_TEXTURE_2D = 0x0DE1
GL_RGBA = 0x1908
GL_RGBA8 = 0x8058
GL_UNSIGNED_BYTE = 0x1401
GL_TEXTURE_MIN_FILTER, GL_TEXTURE_MAG_FILTER = 0x2801, 0x2800
GL_TEXTURE_WRAP_S, GL_TEXTURE_WRAP_T = 0x2802, 0x2803
GL_LINEAR = 0x2601
GL_CLAMP_TO_EDGE = 0x812F
GL_UNPACK_ALIGNMENT = 0x0CF5
GL_UNPACK_ROW_LENGTH = 0x0CF2
GL_UNPACK_SKIP_ROWS = 0x0CF3
GL_UNPACK_SKIP_PIXELS = 0x0CF4
GL_PIXEL_UNPACK_BUFFER = 0x88EC
GL_VENDOR, GL_RENDERER, GL_VERSION = 0x1F00, 0x1F01, 0x1F02
GL_NO_ERROR = 0

MAX_DRAINED_ERRORS = 16

log = logging.getLogger(__name__)


class GlError(Exception):
    pass


def flip_rows(buf, width, height):
    """A top-down RGBA8 raster (width*height*4 bytes) with its rows reversed. OpenGL's origin is the
    bottom-left corner, so this keeps the image upright in the overlay."""
    stride = width * 4
    expected = stride * height
    if len(buf) != expected:
        raise ValueError("raster is %d bytes, expected %d" % (len(buf), expected))
    view = memoryview(buf)
    return b"".join(view[(height - 1 - y) * stride:(height - y) * stride] for y in range(height))


def _load(library):
    """A library by soname, or an already loaded library object (tests pass fakes)."""
    if not isinstance(library, str):
        return library
    try:
        return ctypes.CDLL(library)
    except OSError as e:
        raise GlError("%s not loadable: %s" % (library, e)) from e


class GlTexture:
    """One RGBA8 texture of width x height in a surfaceless EGL/GLES context of the calling thread."""

    def __init__(self, width, height, egl_library="libEGL.so.1", gles_library="libGLESv2.so.2"):
        self.width, self.height = width, height
        self.thread = threading.get_ident()
        self.egl = None
        self.gl = None
        self.display = None
        self.context = None
        self.texture = 0
        self.client_version = 0
        self.storage = "?"
        self.info = "?"
        self.stale_errors = 0  # GL errors found pending before an upload (left by SteamVR's copy)
        try:
            self.egl = _load(egl_library)
            self.gl = _load(gles_library)
            self._prototype()
            self._init_egl()
            self._init_texture()
        except BaseException:
            self.close()
            raise

    # -- setup

    def _prototype(self):
        egl, gl = self.egl, self.gl
        try:
            egl.eglGetProcAddress.restype = c_void_p
            egl.eglGetProcAddress.argtypes = [c_char_p]
            egl.eglGetError.restype = c_int
            egl.eglQueryString.restype = c_char_p
            egl.eglQueryString.argtypes = [c_void_p, c_int]
            egl.eglGetDisplay.restype = c_void_p
            egl.eglGetDisplay.argtypes = [c_void_p]
            egl.eglInitialize.restype = c_uint
            egl.eglInitialize.argtypes = [c_void_p, POINTER(c_int), POINTER(c_int)]
            egl.eglBindAPI.restype = c_uint
            egl.eglBindAPI.argtypes = [c_uint]
            egl.eglChooseConfig.restype = c_uint
            egl.eglChooseConfig.argtypes = [c_void_p, POINTER(c_int), POINTER(c_void_p), c_int, POINTER(c_int)]
            egl.eglCreateContext.restype = c_void_p
            egl.eglCreateContext.argtypes = [c_void_p, c_void_p, c_void_p, POINTER(c_int)]
            egl.eglMakeCurrent.restype = c_uint
            egl.eglMakeCurrent.argtypes = [c_void_p, c_void_p, c_void_p, c_void_p]
            egl.eglGetCurrentContext.restype = c_void_p
            egl.eglGetCurrentContext.argtypes = []
            egl.eglDestroyContext.restype = c_uint
            egl.eglDestroyContext.argtypes = [c_void_p, c_void_p]
            egl.eglTerminate.restype = c_uint
            egl.eglTerminate.argtypes = [c_void_p]
            gl.glGetString.restype = c_char_p
            gl.glGetString.argtypes = [c_uint]
            gl.glGetError.restype = c_uint
            gl.glGetError.argtypes = []
            gl.glGenTextures.argtypes = [c_int, POINTER(c_uint)]
            gl.glDeleteTextures.argtypes = [c_int, POINTER(c_uint)]
            gl.glBindTexture.argtypes = [c_uint, c_uint]
            gl.glBindBuffer.argtypes = [c_uint, c_uint]
            gl.glTexParameteri.argtypes = [c_uint, c_uint, c_int]
            gl.glPixelStorei.argtypes = [c_uint, c_int]
            gl.glTexImage2D.argtypes = [c_uint, c_int, c_int, c_int, c_int, c_int, c_uint, c_uint, c_void_p]
            gl.glTexSubImage2D.argtypes = [c_uint, c_int, c_int, c_int, c_int, c_int, c_uint, c_uint, c_void_p]
            gl.glFlush.argtypes = []
            gl.glFinish.argtypes = []
        except AttributeError as e:  # a symbol is missing from the library
            raise GlError("EGL/GLES entry point missing: %s" % e) from e

    def _init_egl(self):
        egl = self.egl
        display = None
        get_platform_display = egl.eglGetProcAddress(b"eglGetPlatformDisplayEXT")
        if get_platform_display:
            fn = ctypes.CFUNCTYPE(c_void_p, c_uint, c_void_p, POINTER(c_int))(get_platform_display)
            display = fn(EGL_PLATFORM_SURFACELESS_MESA, None, None)
        if not display:
            display = egl.eglGetDisplay(None)
        if not display:
            raise GlError("no EGL display (surfaceless platform unavailable)")
        major, minor = c_int(), c_int()
        if not egl.eglInitialize(display, byref(major), byref(minor)):
            raise GlError("eglInitialize failed: 0x%x" % egl.eglGetError())
        self.display = display
        if not egl.eglBindAPI(EGL_OPENGL_ES_API):
            raise GlError("eglBindAPI(GLES) failed: 0x%x" % egl.eglGetError())
        config = c_void_p()
        count = c_int()
        attribs = (c_int * 13)(EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT, EGL_SURFACE_TYPE, EGL_PBUFFER_BIT,
                               EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8, EGL_NONE)
        if egl.eglChooseConfig(display, attribs, byref(config), 1, byref(count)) == EGL_FALSE:
            raise GlError("eglChooseConfig failed: 0x%x" % egl.eglGetError())
        cfg = config if count.value > 0 else None   # no matching config: EGL_KHR_no_config_context
        for version in (3, 2):
            ctx_attribs = (c_int * 3)(EGL_CONTEXT_CLIENT_VERSION, version, EGL_NONE)
            context = egl.eglCreateContext(display, cfg, None, ctx_attribs)
            if context:
                # stored before eglMakeCurrent, so close() destroys it if making it current fails
                self.context = context
                self.client_version = version
                break
        if not self.context:
            raise GlError("eglCreateContext failed: 0x%x" % egl.eglGetError())
        if not egl.eglMakeCurrent(display, None, None, self.context):
            raise GlError("eglMakeCurrent (surfaceless) failed: 0x%x" % egl.eglGetError())
        self.info = "%s / %s" % ((self.gl.glGetString(GL_RENDERER) or b"?").decode(errors="replace"),
                                 (self.gl.glGetString(GL_VERSION) or b"?").decode(errors="replace"))

    def _tex_storage_2d(self):
        """glTexStorage2D (GLES 3.0), from the library or through eglGetProcAddress; None if absent."""
        fn = getattr(self.gl, "glTexStorage2D", None)
        if fn is not None:
            fn.restype = None
            fn.argtypes = [c_uint, c_int, c_uint, c_int, c_int]
            return fn
        address = self.egl.eglGetProcAddress(b"glTexStorage2D")
        if not address:
            return None
        return ctypes.CFUNCTYPE(None, c_uint, c_int, c_uint, c_int, c_int)(address)

    def _init_texture(self):
        gl = self.gl
        self._drain_errors("before the texture was created")
        tex = c_uint()
        gl.glGenTextures(1, byref(tex))
        if not tex.value:
            raise GlError("glGenTextures returned no texture: 0x%x" % gl.glGetError())
        self.texture = tex.value
        gl.glBindTexture(GL_TEXTURE_2D, self.texture)
        gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        gl.glPixelStorei(GL_UNPACK_ALIGNMENT, 4)
        tex_storage = self._tex_storage_2d() if self.client_version >= 3 else None
        if tex_storage is not None:
            # immutable, sized storage: the size and format never change for the process lifetime
            tex_storage(GL_TEXTURE_2D, 1, GL_RGBA8, self.width, self.height)
            err = gl.glGetError()
            if err != GL_NO_ERROR:
                raise GlError("glTexStorage2D(RGBA8) failed: 0x%x" % err)
            self.storage = "TexStorage2D RGBA8"
        else:
            gl.glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, self.width, self.height, 0, GL_RGBA, GL_UNSIGNED_BYTE, None)
            err = gl.glGetError()
            self.storage = "TexImage2D RGBA8"
            if err != GL_NO_ERROR:
                # a plain ES2 context without OES_required_internalformat only takes the unsized format
                gl.glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, self.width, self.height, 0, GL_RGBA, GL_UNSIGNED_BYTE,
                                None)
                err = gl.glGetError()
                if err != GL_NO_ERROR:
                    raise GlError("glTexImage2D failed: 0x%x" % err)
                self.storage = "TexImage2D RGBA (ES2)"
        self.info = "%s, GLES %d context, %s" % (self.info, self.client_version, self.storage)

    # -- helpers

    def _check_thread(self, what):
        current = threading.get_ident()
        if current != self.thread:
            raise GlError("%s() called from thread %d; the GL context belongs to thread %d"
                          % (what, current, self.thread))

    def _make_current(self):
        if self.context is None:
            raise GlError("GL texture is closed")
        current = self.egl.eglGetCurrentContext()
        if current != self.context:
            log.debug("EGL context not current (0x%x); making it current again", current or 0)
            if not self.egl.eglMakeCurrent(self.display, None, None, self.context):
                raise GlError("eglMakeCurrent failed: 0x%x" % self.egl.eglGetError())

    def _drain_errors(self, what):
        codes = []
        for _ in range(MAX_DRAINED_ERRORS):
            err = self.gl.glGetError()
            if err == GL_NO_ERROR:
                break
            codes.append(err)
        if codes:
            self.stale_errors += len(codes)
            log.debug("GL error(s) %s %s", ", ".join("0x%x" % code for code in codes), what)
        return codes

    # -- per frame

    def upload(self, buf):
        """Copies a top-down RGBA8 raster (width*height*4 bytes) into the texture."""
        self._check_thread("upload")
        flipped = flip_rows(buf, self.width, self.height)
        self._make_current()
        self._drain_errors("left by SetOverlayTexture")
        gl = self.gl
        # SteamVR's client library runs in this process with our context current; do not rely on
        # the unpack state it leaves behind.
        if self.client_version >= 3:
            gl.glBindBuffer(GL_PIXEL_UNPACK_BUFFER, 0)
            gl.glPixelStorei(GL_UNPACK_ROW_LENGTH, 0)
            gl.glPixelStorei(GL_UNPACK_SKIP_ROWS, 0)
            gl.glPixelStorei(GL_UNPACK_SKIP_PIXELS, 0)
        gl.glPixelStorei(GL_UNPACK_ALIGNMENT, 4)
        gl.glBindTexture(GL_TEXTURE_2D, self.texture)
        gl.glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, self.width, self.height, GL_RGBA, GL_UNSIGNED_BYTE, flipped)
        gl.glFlush()
        err = gl.glGetError()
        if err != GL_NO_ERROR:
            raise GlError("glTexSubImage2D failed: 0x%x" % err)

    def finish(self):
        """Waits until the GPU has executed everything issued on this context (before teardown)."""
        self._check_thread("finish")
        self._make_current()
        self.gl.glFinish()

    def close(self, terminate=False):
        """Deletes the texture and the context. Same thread only; safe on a partially initialised
        object and when called twice. eglTerminate only with terminate=True (see the module doc)."""
        self._check_thread("close")
        egl, gl = self.egl, self.gl
        if egl is None or not self.display:
            self.texture = 0
            self.context = None
            return
        if self.context and self.texture and gl is not None:
            try:
                if egl.eglGetCurrentContext() != self.context:
                    egl.eglMakeCurrent(self.display, None, None, self.context)
                tex = c_uint(self.texture)
                gl.glDeleteTextures(1, byref(tex))
            except Exception as e:  # best effort on shutdown
                log.debug("glDeleteTextures: %s", e)
        self.texture = 0
        try:
            egl.eglMakeCurrent(self.display, None, None, None)
        except Exception as e:
            log.debug("eglMakeCurrent(none): %s", e)
        if self.context:
            try:
                egl.eglDestroyContext(self.display, self.context)
            except Exception as e:
                log.debug("eglDestroyContext: %s", e)
            self.context = None
        if terminate:
            try:
                egl.eglTerminate(self.display)
            except Exception as e:
                log.debug("eglTerminate: %s", e)
            self.display = None
