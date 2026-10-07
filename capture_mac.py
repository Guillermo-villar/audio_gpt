"""Captura del audio del sistema en macOS (sys.platform == "darwin").

macOS no tiene un «loopback» como el WASAPI de Windows: aquí se usa
ScreenCaptureKit (macOS 13+), que captura la mezcla de audio del sistema
sin instalar drivers — vía pyobjc, sin código nativo que compilar.

Permiso necesario: «Grabación de pantalla y audio del sistema»
(Ajustes del Sistema → Privacidad y seguridad). Si falta, el primer
intento de captura lanza el aviso de macOS.

Respaldo sin ScreenCaptureKit: instala un dispositivo virtual como
BlackHole (https://existential.audio/blackhole/) y selecciónalo como
entrada en «Dispositivos…» — la app lo ve como un micrófono más.
"""

import ctypes
import threading
import time

import numpy as np

try:
    import objc
    import ScreenCaptureKit as SCK
    import CoreMedia as CM
    import libdispatch

    _SCK_AVAILABLE = True
except Exception:  # pyobjc no instalado o macOS < 13
    _SCK_AVAILABLE = False

DEFAULT_SAMPLERATE = 48000

_cm = None
_cf = None


def _load_cfuncs():
    """Punteros ctypes a CoreMedia/CoreFoundation para leer el PCM del
    CMSampleBuffer (AudioBufferList → numpy). Solo se cargan una vez."""
    global _cm, _cf
    if _cm is not None:
        return
    _cm = ctypes.CDLL(
        "/System/Library/Frameworks/CoreMedia.framework/CoreMedia")
    _cf = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    f = _cm.CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer
    f.restype = ctypes.c_int32
    f.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                  ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p,
                  ctypes.c_uint32, ctypes.c_void_p]
    _cm.CMSampleBufferIsValid.restype = ctypes.c_bool
    _cm.CMSampleBufferIsValid.argtypes = [ctypes.c_void_p]
    _cf.CFRelease.restype = None
    _cf.CFRelease.argtypes = [ctypes.c_void_p]


class _AudioBuffer(ctypes.Structure):
    _fields_ = [("mNumberChannels", ctypes.c_uint32),
                ("mDataByteSize", ctypes.c_uint32),
                ("mData", ctypes.c_void_p)]


class _AudioBufferList(ctypes.Structure):
    _fields_ = [("mNumberBuffers", ctypes.c_uint32),
                ("mBuffers", _AudioBuffer * 1)]


def _pcm_from_sample_buffer(sbuf):
    """CMSampleBuffer de audio → np.float32 (frames, canales).

    En macOS recientes el estéreo llega NO entrelazado (un AudioBuffer por
    canal); aquí se aceptan ambos layouts. Devuelve None si no hay datos.
    """
    sbuf_ptr = objc.pyobjc_id(sbuf)
    needed = ctypes.c_size_t(0)
    flags = CM.kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment
    st = _cm.CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
        sbuf_ptr, ctypes.byref(needed), None, 0, None, None, flags, None)
    if st != 0 or needed.value == 0:
        return None
    raw = ctypes.create_string_buffer(needed.value)
    blockbuf = ctypes.c_void_p()
    st = _cm.CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
        sbuf_ptr, None, raw, needed.value, None, None, flags,
        ctypes.byref(blockbuf))
    try:
        if st != 0:
            return None
        abl = _AudioBufferList.from_address(ctypes.addressof(raw))
        nbuf = abl.mNumberBuffers
        if nbuf == 0:
            return None
        bufs = ctypes.cast(ctypes.addressof(abl.mBuffers),
                           ctypes.POINTER(_AudioBuffer * nbuf)).contents
        parts, nch = [], 0
        for b in bufs:
            if not b.mData or b.mDataByteSize == 0:
                continue
            arr = np.frombuffer(ctypes.string_at(b.mData, b.mDataByteSize),
                                dtype=np.float32)
            parts.append((b.mNumberChannels or 1, arr))
            nch += b.mNumberChannels or 1
        if not parts:
            return None
        if len(parts) == 1:
            ch, arr = parts[0]
            return arr.reshape(-1, ch) if ch > 1 else arr.reshape(-1, 1)
        # no entrelazado: cada AudioBuffer es un canal
        frames = min(len(a) for _, a in parts)
        out = np.empty((frames, nch), dtype=np.float32)
        for i, (_, a) in enumerate(parts):
            out[:, i] = a[:frames]
        return out
    finally:
        if blockbuf.value:
            _cf.CFRelease(blockbuf)


def sck_available():
    """True si ScreenCaptureKit se puede usar (pyobjc + macOS ≥ 13)."""
    return _SCK_AVAILABLE


def screen_capture_preflight():
    """True si el permiso de grabación de pantalla ya está concedido."""
    if not _SCK_AVAILABLE:
        return False
    try:
        import Quartz
        return bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception:
        return True   # API ausente: no podemos saberlo, lo verá el stream


def request_screen_capture():
    """Pide el permiso de grabación (macOS muestra su diálogo)."""
    if not _SCK_AVAILABLE:
        return False
    try:
        import Quartz
        return bool(Quartz.CGRequestScreenCaptureAccess())
    except Exception:
        return False


def open_screen_capture_settings():
    """Abre Ajustes del Sistema en el panel de grabación de pantalla."""
    import subprocess
    for url in (
        # macOS 13+ : «Grabación de pantalla y audio del sistema»
        "x-apple.systempreferences:com.apple.preference.security"
        "?Privacy_ScreenCapture",
    ):
        try:
            subprocess.Popen(["open", url])
            return
        except Exception:
            pass


PERMISSION_HINT = (
    "Ajustes del Sistema → Privacidad y seguridad → "
    "«Grabación de pantalla y audio del sistema» → activa esta app"
)


if _SCK_AVAILABLE:
    from Foundation import NSObject

    class _AudioDelegate(NSObject):
        """SCStreamOutput: recibe los CMSampleBuffer de audio y los
        convierte a bloques float32 para una cola thread-safe."""

        def init(self):
            self = objc.super(_AudioDelegate, self).init()
            self.on_block = None        # callable(np.ndarray (n, ch))
            self.on_error = None        # callable(str)
            return self

        def stream_didOutputSampleBuffer_ofType_(self, stream, sbuf, otype):
            if otype != SCK.SCStreamOutputTypeAudio:
                return
            try:
                if not _cm.CMSampleBufferIsValid(objc.pyobjc_id(sbuf)):
                    return
                pcm = _pcm_from_sample_buffer(sbuf)
                if pcm is not None and pcm.size and self.on_block:
                    self.on_block(pcm)
            except Exception as e:  # nunca lanzar dentro del callback
                if self.on_error:
                    self.on_error(repr(e))

        def stream_didFailWithError_(self, stream, error):
            if self.on_error:
                self.on_error(str(error))


class SystemAudioRecorder:
    """Misma interfaz que capture.LoopbackRecorder pero sobre
    ScreenCaptureKit: start(), read() bloqueante de un bloque,
    blocks(running) generador y close().

    `read()` devuelve float32 [-1, 1] con `block_frames` frames y
    `channels` canales. Internamente acumula los trozos que entrega SCK
    (~20 ms cada uno) hasta completar el bloque pedido.
    """

    def __init__(self, device=None, samplerate=DEFAULT_SAMPLERATE,
                 channels=2, block_ms=100):
        if not _SCK_AVAILABLE:
            raise RuntimeError(
                "ScreenCaptureKit no está disponible: instala los paquetes "
                "pyobjc (requirements.txt) en macOS 13 o superior.")
        self.samplerate = samplerate
        self.channels = channels
        self.block_frames = int(samplerate * block_ms / 1000)
        self._stream = None
        self._delegate = None
        self._buf = np.zeros((0, channels), dtype=np.float32)
        self._buf_cv = threading.Condition()
        self._started = threading.Event()
        self._error = None
        self._closed = False
        self._nblocks = 0

    # ------------------------- callbacks SCK -------------------------

    def _on_block(self, pcm):
        self._nblocks += 1
        with self._buf_cv:
            # adapta canales: mono→duplica, >2→recorta
            if pcm.shape[1] < self.channels:
                pcm = np.repeat(pcm, self.channels, axis=1)
            elif pcm.shape[1] > self.channels:
                pcm = pcm[:, :self.channels]
            self._buf = np.concatenate((self._buf, pcm))
            self._buf_cv.notify_all()

    def _on_error(self, msg):
        self._error = msg
        self._started.set()             # desbloquea start()
        with self._buf_cv:
            self._buf_cv.notify_all()   # desbloquea read()

    # ------------------------- ciclo de vida -------------------------

    def start(self):
        _load_cfuncs()
        self._closed = False
        got = threading.Event()
        holder = {}

        def _content_cb(content, error):
            holder["content"], holder["error"] = content, error
            got.set()

        SCK.SCShareableContent.getShareableContentWithCompletionHandler_(
            _content_cb)
        if not got.wait(10) or holder.get("content") is None:
            raise RuntimeError(
                "macOS no deja enumerar el contenido compartible. "
                "Concede el permiso en " + PERMISSION_HINT)
        displays = holder["content"].displays()
        if displays.count() == 0:
            raise RuntimeError("No se encontró ninguna pantalla que capturar.")

        filt = SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(
            displays[0], [])
        conf = SCK.SCStreamConfiguration.alloc().init()
        conf.setCapturesAudio_(True)
        conf.setExcludesCurrentProcessAudio_(True)
        conf.setSampleRate_(self.samplerate)
        conf.setChannelCount_(self.channels)
        conf.setShowsCursor_(False)

        self._delegate = _AudioDelegate.alloc().init()
        self._delegate.on_block = self._on_block
        self._delegate.on_error = self._on_error
        self._stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(
            filt, conf, self._delegate)
        queue = libdispatch.dispatch_get_global_queue(21, 0)  # USER_INITIATED
        ok, err = self._stream.addStreamOutput_type_sampleHandlerQueue_error_(
            self._delegate, SCK.SCStreamOutputTypeAudio, queue, None)
        if not ok:
            raise RuntimeError(f"No se pudo añadir la salida de audio: {err}")

        done = threading.Event()

        def _start_cb(error):
            if error is not None:
                self._error = str(error)
            done.set()

        self._stream.startCaptureWithCompletionHandler_(_start_cb)
        if not done.wait(10):
            raise RuntimeError("ScreenCaptureKit no respondió al arrancar.")
        if self._error:
            raise RuntimeError(
                "ScreenCaptureKit falló al iniciar: "
                f"{self._error}. Revisa {PERMISSION_HINT}")
        self._started.set()

    def read(self):
        """Un bloque float32 (block_frames, channels); bloqueante."""
        deadline = time.monotonic() + 30
        with self._buf_cv:
            while len(self._buf) < self.block_frames:
                if self._error or self._closed:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._buf_cv.wait(remaining)
            if len(self._buf) < self.block_frames:
                if self._error:
                    raise RuntimeError(
                        f"Captura interrumpida: {self._error}. "
                        f"Revisa {PERMISSION_HINT}")
                return np.zeros((0, self.channels), dtype=np.float32)
            block = self._buf[:self.block_frames]
            self._buf = self._buf[self.block_frames:]
            return block

    def blocks(self, running=lambda: True):
        self.start()
        try:
            while running():
                yield self.read()
        finally:
            self.close()

    def close(self):
        self._closed = True
        with self._buf_cv:
            self._buf_cv.notify_all()
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                done = threading.Event()
                stream.stopCaptureWithCompletionHandler_(
                    lambda error: done.set())
                done.wait(5)
            except Exception:
                pass
        self._delegate = None


def default_recorder(samplerate=DEFAULT_SAMPLERATE, channels=2,
                     block_ms=100):
    """ScreenAudioRecorder listo para usar, o RuntimeError."""
    return SystemAudioRecorder(samplerate=samplerate, channels=channels,
                               block_ms=block_ms)


def status_text():
    """Resumen para el diálogo «Dispositivos…» (una línea, español)."""
    if not _SCK_AVAILABLE:
        return ("No disponible: faltan los paquetes pyobjc de "
                "requirements.txt (o macOS < 13).")
    if screen_capture_preflight():
        return "OK — ScreenCaptureKit captura la mezcla de audio del sistema."
    return ("Sin permiso todavía. Concédelo en " + PERMISSION_HINT +
            " (el primer intento de captura también lanza el aviso de macOS).")
