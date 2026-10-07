"""Piezas específicas de macOS para audio_gpt (sys.platform == "darwin").

- Hotkeys globales con un Quartz CGEventTap: intercepta las combinaciones
  propias y las consume para que no lleguen al navegador (igual que el
  WH_KEYBOARD_LL de Windows). Necesita el permiso de **Accesibilidad**
  (Ajustes del Sistema → Privacidad y seguridad → Accesibilidad); para
  escuchar también basta «Monitorización de entrada», pero para *consumir*
  teclas hace falta Accesibilidad.
- Exclusión de captura vía NSWindow.sharingType = NSWindowSharingNone.
  OJO: desde macOS 15.4 ScreenCaptureKit ignora este flag (confirmado por
  Apple DTS / electron#48258) — sigue ocultando la ventana a capturas
  antiguas (CGWindowList, VNC), pero NO prometer privacidad total en
  macOS ≥ 15.
- Controles mantenidos del overlay (Ctrl+flechas, Ctrl+±): el mismo tap
  lleva el estado de teclas, así que no hace falta un GetAsyncKeyState.
"""

import sys
import threading

if sys.platform != "darwin":
    raise ImportError("platform_mac solo funciona en macOS")

import objc
import Quartz

# Resolver los símbolos que usarán los taps AHORA (hilo principal): el
# lazy-import de pyobjc falla con KeyError si dos hilos lo disparan a la vez.
for _n in ("CGEventGetIntegerValueField", "CGEventGetFlags",
           "CGEventTapCreate", "CGEventTapEnable", "CGEventMaskBit",
           "CFMachPortCreateRunLoopSource", "CFMachPortInvalidate",
           "CFRunLoopGetCurrent", "CFRunLoopAddSource",
           "CGEventSourceFlagsState", "CGEventSourceKeyState",
           "CGPreflightListenEventAccess", "CGPreflightScreenCaptureAccess",
           "CGRequestScreenCaptureAccess", "CGRequestListenEventAccess"):
    getattr(Quartz, _n, None)

try:
    import ApplicationServices as _AS
except Exception:
    _AS = None


# ---------------------------------------------------------------------------
# Hotkeys globales (CGEventTap)
# ---------------------------------------------------------------------------
# Keycodes ANSI de macOS (no son los VK de Windows):
#   Q=0x0C  S=0x01  M=0x2E  I=0x22  G=0x05  T=0x11
#   flechas: ←0x7B ↑0x7E →0x7C ↓0x7D ; '+' = 0x18 (con shift) / 0x45 keypad ;
#   '-' = 0x1B / 0x4E keypad.
# Combos = mismos que en Windows (Alt = Option ⌥, Ctrl = Control ⌃).
MAC_HOTKEYS = {
    "ctrl+q": (Quartz.kCGEventFlagMaskControl, 0x0C),
    "alt+s": (Quartz.kCGEventFlagMaskAlternate, 0x01),
    "ctrl+m": (Quartz.kCGEventFlagMaskControl, 0x2E),
    "ctrl+i": (Quartz.kCGEventFlagMaskControl, 0x22),
    "alt+g": (Quartz.kCGEventFlagMaskAlternate, 0x05),
    "alt+t": (Quartz.kCGEventFlagMaskAlternate, 0x11),
}

_ARROW_KC = {0x7B: (-1, 0), 0x7E: (0, -1), 0x7C: (1, 0), 0x7D: (0, 1)}
_PLUS_KC = (0x18, 0x45)      # '=' (⇧='+' ) y '+' del teclado numérico
_MINUS_KC = (0x1B, 0x4E)     # '-' y '-' del teclado numérico

_CTRL_ALT_CMD = (Quartz.kCGEventFlagMaskControl
                 | Quartz.kCGEventFlagMaskAlternate
                 | Quartz.kCGEventFlagMaskCommand)

_overlay_active = None      # callable() -> bool, lo fija la GUI
_pressed = {}               # keycode -> flags del keydown (los ve el tap)
_pressed_lock = threading.Lock()


def set_overlay_active(callback):
    """La GUI registra aquí cómo saber si el overlay está visible: sirve
    para tragar Ctrl+flechas/± (así no llegan a Mission Control)."""
    global _overlay_active
    _overlay_active = callback


def accessibility_trusted():
    """True si el proceso ya tiene el permiso de Accesibilidad."""
    if _AS is None:
        return False
    try:
        return bool(_AS.AXIsProcessTrusted())
    except Exception:
        return False


def request_accessibility():
    """Lanza el diálogo del sistema para conceder Accesibilidad."""
    if _AS is None:
        return False
    try:
        return bool(_AS.AXIsProcessTrustedWithOptions(
            {_AS.kAXTrustedCheckOptionPrompt: True}))
    except Exception:
        return False


def open_accessibility_settings():
    import subprocess
    try:
        subprocess.Popen(
            ["open", "x-apple.systempreferences:com.apple.preference."
                     "security?Privacy_Accessibility"])
    except Exception:
        pass


ACCESSIBILITY_HINT = (
    "Ajustes del Sistema → Privacidad y seguridad → Accesibilidad → "
    "activa esta app (quizá tengas que pulsar «+» y elegirla en "
    "Aplicaciones). Reinicia la app después."
)


def _is_injected(event):
    """Eventos sintéticos (CGEventPost): tienen pid de origen > 0.
    Igual que el LLKHF_INJECTED de Windows: se dejan pasar sin tocar."""
    try:
        pid = Quartz.CGEventGetIntegerValueField(
            event, Quartz.kCGEventSourceUnixProcessID)
        return pid != 0
    except Exception:
        return False


def install_hotkeys(dispatch):
    """Instala el tap de teclado en un hilo con CFRunLoop propio.

    `dispatch` recibe el combo («ctrl+q», …) — la GUI lo emite como señal
    Qt encolada. Devuelve un handle para stop_hotkeys() o None si no se
    pudo (sin permiso de Accesibilidad el tap no se crea).
    """
    mask = (Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
            | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp))
    fired = set()
    holder = {"taps": [], "ready": threading.Event()}

    def _callback(_proxy, type_, event, allow_injected):
        # El sistema puede deshabilitar el tap por timeout: reactivarlo.
        if type_ in (Quartz.kCGEventTapDisabledByTimeout,
                     Quartz.kCGEventTapDisabledByUserInput):
            for tap in holder["taps"]:
                Quartz.CGEventTapEnable(tap, True)
            return event

        kc = Quartz.CGEventGetIntegerValueField(
            event, Quartz.kCGKeyboardEventKeycode)
        flags = Quartz.CGEventGetFlags(event)

        if type_ == Quartz.kCGEventKeyDown:
            with _pressed_lock:
                _pressed[kc] = flags
            if not allow_injected and _is_injected(event):
                return event
            ctrl = bool(flags & Quartz.kCGEventFlagMaskControl)
            alt = bool(flags & Quartz.kCGEventFlagMaskAlternate)
            # Exclusividad como en Windows: el modificador pedido sí, los
            # otros dos no (Ctrl+Q ≠ Cmd+Ctrl+Q: no hay que tragar el
            # bloqueo de pantalla).
            mods = flags & _CTRL_ALT_CMD
            for combo, (mod_mask, key) in MAC_HOTKEYS.items():
                if kc != key or mods != mod_mask:
                    continue
                if combo in fired:
                    return None           # autorepetición: consumir igual
                fired.add(combo)
                try:
                    dispatch(combo)
                except Exception:
                    pass
                return None               # se consume: no llega al navegador
            # Controles del overlay: solo con el panel visible y Ctrl
            # (sin Cmd/Option) — se tragan para no activar Mission Control.
            if (ctrl and not alt
                    and not (flags & Quartz.kCGEventFlagMaskCommand)
                    and _overlay_active is not None
                    and _overlay_active()
                    and kc in (*_ARROW_KC, *_PLUS_KC, *_MINUS_KC)):
                return None
            return event

        if type_ == Quartz.kCGEventKeyUp:
            with _pressed_lock:
                _pressed.pop(kc, None)
            if not allow_injected and _is_injected(event):
                return event
            for combo, (_, key) in MAC_HOTKEYS.items():
                if kc == key and combo in fired:
                    fired.discard(combo)
                    return None           # el keyup también se consume
            return event

        return event

    def _run():
        rl = Quartz.CFRunLoopGetCurrent()
        # Dos puntos de escucha con el mismo callback:
        #  · kCGHIDEventTap — teclado físico en Macs reales; aquí los eventos
        #    inyectados se dejan pasar como en Windows (LLKHF_INJECTED).
        #  · kCGSessionEventTap — VMs (p. ej. Apple Virtualization: su
        #    daemon de entrada publica a nivel de sesión con pid != 0) y
        #    herramientas que postean eventos; aquí sí se despachan los
        #    combos aunque sean inyectados, para que el atajo funcione.
        for location, allow_injected in (
                (Quartz.kCGHIDEventTap, False),
                (Quartz.kCGSessionEventTap, True)):
            tap = Quartz.CGEventTapCreate(
                location,
                Quartz.kCGHeadInsertEventTap,
                Quartz.kCGEventTapOptionDefault,  # intercepta (no listen-only)
                mask, _callback, allow_injected)
            if tap is None:
                continue
            holder["taps"].append(tap)
            src = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
            Quartz.CFRunLoopAddSource(rl, src, Quartz.kCFRunLoopCommonModes)
            Quartz.CGEventTapEnable(tap, True)
        holder["ready"].set()
        if not holder["taps"]:
            return
        Quartz.CFRunLoopRun()

    thread = threading.Thread(target=_run, daemon=True,
                              name="audio_gpt-hotkeys")
    thread.start()
    # esperar a que los taps existan o fallen (permiso denegado → lista vacía)
    holder["ready"].wait(1.0)
    if not holder["taps"]:
        return None
    return {"taps": holder["taps"], "thread": thread}


def stop_hotkeys(handle):
    """Desactiva los taps instalados por install_hotkeys()."""
    if not handle:
        return
    for tap in handle.get("taps") or ():
        try:
            Quartz.CGEventTapEnable(tap, False)
            Quartz.CFMachPortInvalidate(tap)
        except Exception:
            pass


def overlay_controls():
    """Estado de los controles mantenidos del overlay (lo lee la GUI a
    30 ms): (dx, dy, d_opacity) según las teclas pulsadas AHORA.

    Solo con Ctrl pulsado (ni Option ni Cmd): Ctrl+flechas mueve,
    Ctrl+± cambia opacidad — igual que en Windows.
    """
    with _pressed_lock:
        held = dict(_pressed)
    if not held:
        return (0, 0, 0.0)
    # Ctrl mantenido = alguna tecla pulsada que llegó con flag Control
    # (los eventos inyectados no actualizan FlagsState de forma fiable
    # en VMs; los flags del propio keydown sí los traen).
    mods = 0
    for f in held.values():
        mods |= f & _CTRL_ALT_CMD
    if (not mods & Quartz.kCGEventFlagMaskControl
            or mods & (Quartz.kCGEventFlagMaskAlternate
                       | Quartz.kCGEventFlagMaskCommand)):
        return (0, 0, 0.0)
    dx = dy = 0
    for kc, (x, y) in _ARROW_KC.items():
        if kc in held:
            dx, dy = x, y
            break
    dop = 0.0
    if any(kc in held for kc in _PLUS_KC):
        dop = 1.0
    elif any(kc in held for kc in _MINUS_KC):
        dop = -1.0
    return (dx, dy, dop)


# ---------------------------------------------------------------------------
# Ventanas: exclusión de captura y comportamiento del overlay
# ---------------------------------------------------------------------------

def _nswindow_of(widget):
    """NSWindow de un QWidget (winId() devuelve el NSView*)."""
    try:
        view = objc.objc_object(c_void_p=int(widget.winId()))
        return view.window()
    except Exception:
        return None


def set_capture_exclusion(widget, enabled):
    """NSWindowSharingNone = oculta la ventana a capturas compatibles.

    Limitación honesta: ScreenCaptureKit ignora este flag desde macOS 15.4
    (la ventana SÍ aparece en Zoom/Meet/QuickTime con captura moderna);
    sigue funcionando con capturas antiguas (CGWindowList, VNC) y en
    macOS ≤ 14. Se aplica igualmente porque no cuesta nada.
    """
    if sys.platform != "darwin":
        return
    try:
        import AppKit
        window = _nswindow_of(widget)
        if window is None:
            return
        window.setSharingType_(
            AppKit.NSWindowSharingNone if enabled
            else AppKit.NSWindowSharingReadOnly)
    except Exception:
        pass


def tune_overlay_window(widget):
    """Ajustes del overlay para el modo discreto en macOS:

    - CanJoinAllSpaces + Stationary: visible en todos los escritorios y
      encima de apps a pantalla completa.
    - Nivel NSStatusWindowLevel: flota sobre ventanas normales.
    - Qt.Tool ya evita que aparezca en Cmd+Tab ni icono propio en el Dock.
    - WA_ShowWithoutActivating + WindowDoesNotAcceptFocus (Qt) + la ventana
      no se hace key: no roba el foco del navegador.
    """
    window = _nswindow_of(widget)
    if window is None:
        return
    try:
        import AppKit
        behavior = (AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                    | AppKit.NSWindowCollectionBehaviorStationary
                    | AppKit.NSWindowCollectionBehaviorIgnoresCycle
                    | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
        window.setCollectionBehavior_(behavior)
        window.setLevel_(AppKit.NSStatusWindowLevel)
        window.setHidesOnDeactivate_(False)
    except Exception:
        pass
