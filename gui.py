"""audio_gpt — grabación del audio del sistema, transcripción y respuestas GPT.

Pipeline continuo:
    captura (loopback WASAPI / micrófono / VB-Cable)
      -> VAD (segmentos de habla reales)
      -> transcripción (OpenAI / Groq / local / realtime WebSocket)
      -> GPT opcional por segmento (respuestas automáticas)

Se migra de PyQt5 a PySide6 (Qt6) y se elimina la dependencia de VB-Cable.
"""

import os
import sys
import time
import queue
import uuid
import json
import html
import re
import ctypes
import threading
from collections import deque
from ctypes import wintypes

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QPushButton, QVBoxLayout, QHBoxLayout,
    QWidget, QLabel, QSpinBox, QTextEdit, QLineEdit, QComboBox,
    QProgressBar, QFileDialog, QMessageBox, QGroupBox, QStatusBar,
    QDialog, QDialogButtonBox, QFrame, QSplitter, QCheckBox,
    QPlainTextEdit,
)
from PySide6.QtCore import (
    Qt, QThread, Signal, Slot, QMutex, QTimer, QRect,
    QPropertyAnimation, QEasingCurve,
)
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QTextCursor, QPalette

import numpy as np
import sounddevice as sd
import soundfile as sf

import capture
import transcriber
import vad
from api_client import (
    ApiKeyManager, TranscriptionThread, WhisperService, GptClient,
    GptQueryThread, CodexCliThread, looks_like_question, clef_question,
)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
TRANSCRIPTS_DIR = os.path.join(APP_DIR, "transcripts")

# Hotkeys globales de 2 teclas, vía WH_KEYBOARD_LL (eventos reales de
# tecla, sin admin ni deps). Elegidas porque NO hacen nada en
# Chrome/Edge/Firefox ni ES-keyboards:
# Ctrl+Q responder · Ctrl+M auto-GPT · Ctrl+I panel (único control de
# visibilidad) · Alt+G enviar a GPT · Alt+T start/stop.
# (Ojo: nunca Ctrl+Alt — AltGr en teclado ES = Ctrl+Alt y escribir «@»
# dispararía el hotkey.)
HOTKEYS = {
    "ctrl+q": (0x11, 0x51, "answer_last", "Ctrl+Q · responder última"),
    "ctrl+m": (0x11, 0x4D, "toggle_auto", "Ctrl+M · auto-GPT"),
    "ctrl+i": (0x11, 0x49, "toggle_compact", "Ctrl+I · panel"),
    "alt+g": (0x12, 0x47, "send_gpt", "Alt+G · enviar a GPT"),
    "alt+t": (0x12, 0x54, "toggle_capture", "Alt+T · start/stop"),
}

# Controles mantenidos del overlay (se repiten mientras se pulsan):
# Ctrl+flechas lo mueve por la pantalla · Ctrl+± ajusta su opacidad.
_ARROW_VK = {0x25: (-1, 0), 0x26: (0, -1), 0x27: (1, 0), 0x28: (0, 1)}
_PLUS_VK = (0xBB, 0x6B)      # OEM '+' y '+' del teclado numérico
_MINUS_VK = (0xBD, 0x6D)     # OEM '-' y '-' del teclado numérico
_OVERLAY_MOVE_STEP = 18
_OVERLAY_OPACITY_STEP = 0.05

_SPEAKER_TAG_RE = re.compile(r"&lt;S(\d+)&gt;")
_SPEAKER_COLORS = (
    "#1a56db", "#7a3db8", "#a35c00", "#007a5e", "#a82f5b", "#4c6f91",
)


def _format_transcript_html(text):
    """Escapa HTML sin convertir comillas y colorea marcas <S#>."""
    escaped = html.escape(text, quote=False)

    def repl(match):
        speaker = int(match.group(1))
        color = _SPEAKER_COLORS[speaker % len(_SPEAKER_COLORS)]
        return f'<b style="color:{color}">S{speaker}:</b>'
    return _SPEAKER_TAG_RE.sub(repl, escaped).replace("\n", "<br>")


def _mod_state_ok(mod_vk):
    """Modificador exigido y el otro NO pulsado — excluye AltGr
    (AltGr en teclado ES = Ctrl+Alt, y dispararía ambos)."""
    u32 = ctypes.windll.user32
    ctrl = bool(u32.GetAsyncKeyState(0x11) & 0x8000)
    alt = bool(u32.GetAsyncKeyState(0x12) & 0x8000)
    return (ctrl and not alt) if mod_vk == 0x11 else (alt and not ctrl)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


_HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.c_int, wintypes.WPARAM,
    ctypes.POINTER(KBDLLHOOKSTRUCT))


def _install_ll_hook(dispatch):
    """Hotkeys por EVENTO real de tecla (WH_KEYBOARD_LL).

    El polling de GetAsyncKeyState con el bit 0x0001 («pulsada desde la
    última llamada») colaba pulsaciones ajenas — teclear «i» y luego
    Ctrl+C dentro del mismo sondeo disparaba Ctrl+I y el panel aparecía
    solo. Con el hook cada pulsación llega como evento real: ni taps
    cortos perdidos ni falsos positivos de Ctrl+C/V. Devuelve
    (hook_handle, callback) para retener las referencias, o None.
    """
    if sys.platform != "win32":
        return None
    u32 = ctypes.windll.user32
    WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
    WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
    _fired = set()            # combos ya disparados en esta pulsación

    def proc(ncode, wparam, lparam):
        try:
            if ncode == 0:
                if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    vk = lparam.contents.vkCode
                    for combo, (mod, key, _, _) in HOTKEYS.items():
                        if (vk == key and combo not in _fired
                                and _mod_state_ok(mod)):
                            _fired.add(combo)
                            dispatch(combo)
                elif wparam in (WM_KEYUP, WM_SYSKEYUP):
                    vk = lparam.contents.vkCode
                    for combo, (_, key, _, _) in HOTKEYS.items():
                        if vk == key:
                            _fired.discard(combo)
        except Exception:
            pass
        return u32.CallNextHookEx(None, ncode, wparam, lparam)

    cb = _HOOKPROC(proc)
    hook = u32.SetWindowsHookExW(13, cb, None, 0)   # LL hooks: hMod=None
    if not hook:
        return None
    return hook, cb          # guardar refs: sin ellas el GC mata el hook


# ---------------------------------------------------------------------------
# Overlay compacto — ventana anclable encima de todo durante la llamada
# ---------------------------------------------------------------------------

class CompactOverlay(QWidget):
    """Mini-panel always-on-top (se muestra/oculta SOLO con Ctrl+I).

    Estado, lo que se oye en vivo y la conversación completa con las
    respuestas — el visor hace autoscroll y siempre deja lo reciente a
    la vista. Ctrl+flechas lo mueve por la pantalla, Ctrl+± ajusta la
    opacidad y la altura crece suave según llega texto de GPT.
    """

    WIDTH = 460
    MIN_H = 200
    MAX_H = 560
    MIN_OPACITY = 0.30
    LOG_MAX_BLOCKS = 320          # recorte del historial interno del panel

    def __init__(self):
        super().__init__(
            None,
            Qt.Window | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(self.WIDTH, 240)
        self._drag_pos = None
        self._listening = False
        self._opacity = 0.96
        self.setWindowOpacity(self._opacity)
        self._gpt_anchors = {}     # thread -> {"start","end","header"}

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        panel = QFrame()
        panel.setStyleSheet(
            "QFrame { background: rgba(18,18,22,235); border-radius: 10px; }"
            "QLabel { color: #e8e8e8; }"
            "QTextEdit { background: rgba(255,255,255,18); color: #f2f2f2;"
            "            border: none; border-radius: 6px; }"
        )
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(10, 8, 10, 8)
        panel_layout.setSpacing(4)

        self.status_label = QLabel("● en espera")
        self.status_label.setStyleSheet("color: #9a9a9a; font-size: 11px;")
        panel_layout.addWidget(self.status_label)

        self.live_label = QLabel("")
        self.live_label.setWordWrap(True)
        self.live_label.setStyleSheet("color: #9ecbff; font-size: 12px;")
        self.live_label.setMaximumHeight(34)
        panel_layout.addWidget(self.live_label)

        self.convo_box = QTextEdit()
        self.convo_box.setReadOnly(True)
        self.convo_box.setPlaceholderText("conversación y respuestas…")
        panel_layout.addWidget(self.convo_box, 1)

        legend = QLabel(
            "Ctrl+Q responder · Alt+G enviar · Ctrl+M auto · Alt+T start/stop "
            "· Ctrl+←↑→↓ mover · Ctrl+± opacidad · Ctrl+I ocultar")
        legend.setWordWrap(True)
        legend.setStyleSheet("color: #777; font-size: 10px;")
        panel_layout.addWidget(legend)

        root.addWidget(panel)

        self._ack_timer = QTimer(self)
        self._ack_timer.setSingleShot(True)
        self._ack_timer.timeout.connect(self._restore_status)
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._resize_to_content)

    # ------------------------- estado / ACKs -------------------------

    def _restore_status(self):
        color = "#5ee07a" if self._listening else "#9a9a9a"
        text = "● escuchando" if self._listening else "● en espera"
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color}; font-size: 11px;")

    def set_listening(self, listening):
        self._listening = listening
        if not self._ack_timer.isActive():
            self._restore_status()

    def ack(self, text):
        """Flash de confirmación en la línea de estado (~1.4 s): cada
        comando recibido deja constancia visible de que entró."""
        self.status_label.setText(f"✓ {text}")
        self.status_label.setStyleSheet("color: #ffd166; font-size: 11px;")
        self._ack_timer.start(1400)

    # ------------------------- conversación --------------------------

    def set_live(self, text):
        self.live_label.setText(text[-180:] if text else "")

    def _scroll_bottom(self):
        """Autoscroll: lo último siempre visible sin tocar la rueda."""
        bar = self.convo_box.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _trim_log(self):
        doc = self.convo_box.document()
        excess = doc.blockCount() - self.LOG_MAX_BLOCKS
        if excess <= 0:
            return
        cur = QTextCursor(doc)
        cur.movePosition(QTextCursor.MoveOperation.Start)
        cur.movePosition(QTextCursor.MoveOperation.Down,
                         QTextCursor.MoveMode.KeepAnchor, excess)
        cur.removeSelectedText()

    def add_line(self, tag, text, color):
        safe = html.escape(text, quote=False).replace("\n", "<br>")
        self.convo_box.append(
            f'<b style="color:{color}">{html.escape(tag)}</b> '
            f'<span style="color:#f2f2f2">{safe}</span>')
        self._trim_log()
        self._scroll_bottom()
        self._schedule_resize()

    def add_transcript(self, lane, text):
        color = "#7ee2a8" if lane == "Tú" else "#9ecbff"
        self.add_line(f"{lane}:", text, color)

    def add_note(self, text):
        self.convo_box.append(
            f'<span style="color:#888;font-style:italic">'
            f'{html.escape(text, quote=False)}</span>')
        self._scroll_bottom()

    def gpt_update(self, thread, text, header=None):
        """Bloque GPT editable en el log: el streaming reescribe el mismo
        bloque (no acumula líneas) y el panel crece con él."""
        cur = self.convo_box.textCursor()
        entry = self._gpt_anchors.get(thread)
        if entry is None:
            cur.movePosition(QTextCursor.MoveOperation.End)
            if self.convo_box.document().characterCount() > 1:
                cur.insertText("\n\n")
            entry = {"start": cur.position(), "end": cur.position(),
                     "header": header or "▸ GPT"}
            self._gpt_anchors[thread] = entry
        if header:
            entry["header"] = header
        cur.setPosition(entry["start"])
        cur.setPosition(entry["end"], QTextCursor.MoveMode.KeepAnchor)
        cur.removeSelectedText()
        body = html.escape(text, quote=False).replace("\n", "<br>")
        cur.insertHtml(
            f'<b style="color:#ffd166">'
            f'{html.escape(entry["header"], quote=False)}</b><br>'
            f'<span style="color:#f2f2f2">{body}</span>')
        new_end = cur.position()
        shift = new_end - entry["end"]
        entry["end"] = new_end
        if shift:
            for other, e in self._gpt_anchors.items():
                if other is not thread and e["start"] > entry["start"]:
                    e["start"] += shift
                    e["end"] += shift
        self._trim_log()
        self._scroll_bottom()
        self._schedule_resize()

    def clear_log(self):
        self.convo_box.clear()
        self._gpt_anchors.clear()

    # --------------------- tamaño / opacidad / posición ------------------

    def _schedule_resize(self):
        if not self._resize_timer.isActive():
            self._resize_timer.start(120)

    def _resize_to_content(self):
        """Altura objetivo según el contenido del visor (cap en MAX_H)."""
        doc_h = self.convo_box.document().size().height()
        chrome = 120           # estado + en vivo + leyenda + márgenes
        desired_convo = min(doc_h + 16, 410)
        target = int(max(self.MIN_H, min(chrome + desired_convo, self.MAX_H)))
        if abs(target - self.height()) < 8:
            return
        anim = QPropertyAnimation(self, b"geometry", self)
        anim.setDuration(240)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(self.geometry())
        anim.setEndValue(QRect(self.x(), self.y(), self.width(), target))
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def nudge(self, dx, dy):
        screen = self.screen().availableGeometry()
        x = min(max(self.x() + dx, screen.left() - self.width() + 60),
                screen.right() - 60)
        y = min(max(self.y() + dy, screen.top()), screen.bottom() - 40)
        self.move(x, y)

    def adjust_opacity(self, delta):
        self._opacity = min(1.0, max(self.MIN_OPACITY, self._opacity + delta))
        self.setWindowOpacity(self._opacity)
        self.ack(f"opacidad {int(self._opacity * 100)} %")

    # arrastrar la ventana sin barra de título
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, e):
        if self._drag_pos is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None
    # Sin doble clic: la visibilidad solo la cambia Ctrl+I.


class AckToast(QWidget):
    """Píldora flotante para confirmar comandos cuando el panel está
    oculto: todo hotkey deja constancia visible aunque nada más se vea."""

    def __init__(self):
        super().__init__(
            None,
            Qt.Window | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint
            | Qt.Tool | Qt.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel()
        self.label.setStyleSheet(
            "background: rgba(18,18,22,235); color: #ffd166;"
            "border-radius: 12px; padding: 8px 16px; font-size: 13px;")
        layout.addWidget(self.label)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_ack(self, text):
        self.label.setText(f"✓ {text}")
        self.adjustSize()
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.center().x() - self.width() // 2,
                  screen.top() + 36)
        self.show()
        self.raise_()
        self._timer.start(1300)


def _load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_settings(settings):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        print(f"No se pudo guardar settings.json: {e}")


# ---------------------------------------------------------------------------
# Widgets auxiliares
# ---------------------------------------------------------------------------

class AudioLevelMonitor(QThread):
    """Hilo para monitorear niveles de audio en tiempo real."""

    level_updated = Signal(float)

    def __init__(self, device_index):
        super().__init__()
        self.device_index = device_index
        self.running = False
        self.samplerate = 44100

    def run(self):
        self.running = True

        def callback(indata, frames, time_, status):
            if self.running:
                self.level_updated.emit(float(np.linalg.norm(indata) / np.sqrt(frames)))

        try:
            with sd.InputStream(device=self.device_index, channels=2,
                                callback=callback,
                                blocksize=int(self.samplerate * 0.1),
                                samplerate=self.samplerate):
                while self.running:
                    sd.sleep(100)
        except Exception as e:
            print(f"Error en monitoreo de audio: {e}")

    def stop(self):
        self.running = False


class AudioLevelWidget(QFrame):
    """Barra de nivel de audio."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(30)
        self.setFrameShape(QFrame.StyledPanel)
        self.level = 0.0

    def set_level(self, level):
        self.level = min(level * 5, 1.0)
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(30, 30, 30))
        width = int(self.width() * self.level)
        if self.level < 0.2:
            color = QColor(0, 180, 0)
        elif self.level < 0.6:
            color = QColor(180, 180, 0)
        else:
            color = QColor(180, 0, 0)
        painter.fillRect(0, 0, width, self.height(), color)
        pen = QPen(QColor(100, 100, 100))
        painter.setPen(pen)
        for i in range(1, 10):
            x = int(self.width() * i / 10)
            painter.drawLine(x, 0, x, self.height())


class ApiKeyDialog(QDialog):
    """Diálogo para pedir la API key del proveedor seleccionado."""

    def __init__(self, parent=None, provider="openai"):
        super().__init__(parent)
        self.provider = provider
        info = transcriber.PROVIDERS.get(provider, {})
        self.setWindowTitle(f"Configuración de API Key — {info.get('label', provider)}")
        self.setModal(True)
        self.setFixedSize(520, 220)

        layout = QVBoxLayout(self)
        title = QLabel("Se necesita una API key")
        title.setStyleSheet("font-size: 16px; font-weight: bold; margin-bottom: 10px;")
        layout.addWidget(title)

        urls = {
            "openai": "https://platform.openai.com/api-keys",
            "groq": "https://console.groq.com/keys",
        }
        url = urls.get(provider, urls["openai"])
        desc = QLabel(
            f"Proveedor: {info.get('label', provider)}\n"
            f"Consigue la clave en: {url}\n\n"
            "La clave se guardará localmente (o define la variable de entorno "
            f"{info.get('key_env') or 'OPENAI_API_KEY'})."
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        key_layout = QHBoxLayout()
        key_layout.addWidget(QLabel("API Key:"))
        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.Password)
        self.api_key_input.setPlaceholderText("sk-..." if provider != "groq" else "gsk_...")
        key_layout.addWidget(self.api_key_input)
        layout.addLayout(key_layout)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.validate_and_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.api_key_input.setFocus()

    def validate_and_save(self):
        api_key = self.api_key_input.text().strip()
        if not api_key:
            QMessageBox.warning(self, "Error", "Por favor, introduce una API key válida.")
            return
        if ApiKeyManager.save_api_key(api_key, self.provider):
            self.accept()
        else:
            QMessageBox.critical(self, "Error", "Error al guardar la API key.")

    def get_api_key(self):
        return self.api_key_input.text().strip()


class AudioDeviceSetupDialog(QDialog):
    """Diálogo de diagnóstico de dispositivos de audio."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Dispositivos de audio")
        self.setMinimumWidth(620)
        self.setModal(True)

        layout = QVBoxLayout(self)

        # Estado del loopback WASAPI (la vía principal en Windows)
        loop_group = QGroupBox("Audio del sistema (loopback WASAPI)")
        loop_layout = QVBoxLayout(loop_group)
        loopback = capture.default_loopback()
        if loopback is not None:
            loop_name = capture.loopback_display_name(loopback)
            self.loop_status = QLabel(f"OK — capturando de: {loop_name}")
            self.loop_status.setStyleSheet("color: green; font-weight: bold;")
        else:
            self.loop_status = QLabel(
                "No disponible. En Windows debería aparecer automáticamente; "
                "si no, usa VB-Cable o el micrófono."
            )
            self.loop_status.setStyleSheet("color: red;")
        self.loop_status.setWordWrap(True)
        loop_layout.addWidget(self.loop_status)
        layout.addWidget(loop_group)

        input_group = QGroupBox("Dispositivos de entrada (grabación)")
        input_layout = QHBoxLayout(input_group)
        self.input_devices = QComboBox()
        self.refresh_input_button = QPushButton("Actualizar")
        input_layout.addWidget(QLabel("Dispositivo:"))
        input_layout.addWidget(self.input_devices, 1)
        input_layout.addWidget(self.refresh_input_button)
        layout.addWidget(input_group)

        output_group = QGroupBox("Dispositivos de salida (reproducción)")
        output_layout = QHBoxLayout(output_group)
        self.output_devices = QComboBox()
        self.refresh_output_button = QPushButton("Actualizar")
        output_layout.addWidget(QLabel("Dispositivo:"))
        output_layout.addWidget(self.output_devices, 1)
        output_layout.addWidget(self.refresh_output_button)
        layout.addWidget(output_group)

        monitor_group = QGroupBox("Monitor de nivel")
        monitor_layout = QVBoxLayout(monitor_group)
        self.level_widget = AudioLevelWidget()
        self.monitor_button = QPushButton("Iniciar monitoreo")
        monitor_layout.addWidget(self.level_widget)
        monitor_layout.addWidget(self.monitor_button)
        layout.addWidget(monitor_group)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.refresh_input_button.clicked.connect(self.load_input_devices)
        self.refresh_output_button.clicked.connect(self.load_output_devices)
        self.monitor_button.clicked.connect(self.toggle_monitor)

        self.monitor_thread = None
        self.monitoring = False
        self.load_input_devices()
        self.load_output_devices()

    def load_input_devices(self):
        self.input_devices.clear()
        for idx, name, _ch in capture.list_input_devices():
            self.input_devices.addItem(name, idx)

    def load_output_devices(self):
        self.output_devices.clear()
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] > 0:
                self.output_devices.addItem(d["name"], i)

    def get_selected_devices(self):
        """Devuelve {'input': idx, 'output': idx} de los combos."""
        return {
            "input": self.input_devices.currentData(),
            "output": self.output_devices.currentData(),
        }

    def toggle_monitor(self):
        if not self.monitoring:
            idx = self.input_devices.currentData()
            if idx is None:
                return
            try:
                self.monitor_thread = AudioLevelMonitor(idx)
                self.monitor_thread.level_updated.connect(self.level_widget.set_level)
                self.monitor_thread.start()
                self.monitoring = True
                self.monitor_button.setText("Detener monitoreo")
            except Exception as e:
                QMessageBox.warning(self, "Error", f"No se pudo monitorizar: {e}")
        else:
            if self.monitor_thread:
                self.monitor_thread.stop()
                self.monitor_thread = None
            self.monitoring = False
            self.monitor_button.setText("Iniciar monitoreo")

    def closeEvent(self, event):
        if self.monitor_thread:
            # No destruir el QThread si sigue corriendo
            self.monitor_thread.stop()
            if self.monitor_thread.wait(2000):
                self.monitor_thread = None
        super().closeEvent(event)


class GptResponseDialog(QDialog):
    """Diálogo con la transcripción y la respuesta de GPT."""

    def __init__(self, parent=None, transcription="", gpt_response=""):
        super().__init__(parent)
        self.setWindowTitle("Respuesta de GPT")
        self.resize(800, 600)

        layout = QVBoxLayout(self)
        splitter = QSplitter(Qt.Vertical)

        transcription_group = QGroupBox("Transcripción")
        t_layout = QVBoxLayout(transcription_group)
        self.transcription_text = QTextEdit()
        self.transcription_text.setReadOnly(True)
        self.transcription_text.setPlainText(transcription)
        t_layout.addWidget(self.transcription_text)

        response_group = QGroupBox("Respuesta de GPT")
        r_layout = QVBoxLayout(response_group)
        self.response_text = QTextEdit()
        self.response_text.setReadOnly(True)
        self.response_text.setPlainText(gpt_response)
        r_layout.addWidget(self.response_text)

        splitter.addWidget(transcription_group)
        splitter.addWidget(response_group)
        layout.addWidget(splitter)

        button_layout = QHBoxLayout()
        self.save_button = QPushButton("Guardar respuesta")
        self.save_button.clicked.connect(self.save_response)
        self.close_button = QPushButton("Cerrar")
        self.close_button.clicked.connect(self.accept)
        button_layout.addWidget(self.save_button)
        button_layout.addStretch()
        button_layout.addWidget(self.close_button)
        layout.addLayout(button_layout)

    def save_response(self):
        text = self.response_text.toPlainText()
        if not text:
            QMessageBox.warning(self, "Advertencia", "No hay respuesta para guardar")
            return
        filename, _ = QFileDialog.getSaveFileName(
            self, "Guardar respuesta", "",
            "Archivos de texto (*.txt);;Markdown (*.md);;Todos los archivos (*)",
        )
        if filename:
            try:
                with open(filename, "w", encoding="utf-8") as f:
                    f.write(text)
                QMessageBox.information(self, "Éxito", f"Respuesta guardada en {filename}")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Error al guardar el archivo: {e}")


# ---------------------------------------------------------------------------
# Hilos de trabajo
# ---------------------------------------------------------------------------

class AudioRecorderThread(QThread):
    """Grabación de duración fija desde la fuente seleccionada."""

    update_progress = Signal(int)
    update_level = Signal(float)
    recording_complete = Signal(bool, str)

    SOURCES = ("loopback", "input", "vbcable")

    def __init__(self, filename, duration, source="loopback", device_index=None):
        super().__init__()
        self.filename = filename
        self.duration = duration
        self.source = source
        self.device_index = device_index
        self.samplerate = 48000
        self.channels = 2

    def _open_stream(self):
        """Devuelve (read_fn, close_fn, channels). read_fn(n) -> np.float32."""
        if self.source == "loopback":
            rec = capture.LoopbackRecorder(samplerate=self.samplerate,
                                           channels=self.channels, block_ms=100)
            rec.start()
            return (lambda n: rec.read(), rec.close, self.channels)
        if self.source == "vbcable":
            idx = capture.find_device_by_name("cable output")
            if idx is None:
                raise RuntimeError("No se encontró el dispositivo VB-Cable.")
        else:
            idx = self.device_index
            if idx is None:
                idx = sd.query_devices(kind="input")["index"]

        # Los micros mono no admiten 2 canales: usa los que el dispositivo tenga
        channels = min(2, sd.query_devices(idx)["max_input_channels"])
        stream = sd.InputStream(device=idx, channels=channels,
                                samplerate=self.samplerate)
        stream.start()
        return (lambda n: stream.read(n)[0],
                lambda: (stream.stop(), stream.close()),
                channels)

    def run(self):
        try:
            read, close, channels = self._open_stream()
            total_frames = int(self.duration * self.samplerate)
            audio_buffer = np.zeros((0, channels), dtype=np.float32)
            frames_recorded = 0

            while frames_recorded < total_frames:
                want = min(int(self.samplerate * 0.1), total_frames - frames_recorded)
                chunk = read(want)
                chunk = np.asarray(chunk, dtype=np.float32)[:want]
                if chunk.ndim == 1:
                    chunk = chunk[:, None]
                audio_buffer = np.concatenate((audio_buffer, chunk))
                got = len(chunk)
                frames_recorded += got
                level = float(np.linalg.norm(chunk) / np.sqrt(max(got, 1)))
                self.update_level.emit(level)
                self.update_progress.emit(int(100 * frames_recorded / total_frames))
                if got < want:
                    break
            close()

            audio, silent = capture.normalize(audio_buffer)
            if silent:
                self.recording_complete.emit(
                    False, "La grabación contiene solo silencio. ¿Suena algo en el sistema?"
                )
                return
            sf.write(self.filename, audio, self.samplerate)
            self.recording_complete.emit(True, self.filename)
        except Exception as e:
            self.recording_complete.emit(False, str(e))


class ContinuousCaptureThread(QThread):
    """Captura continua: emite nivel y segmentos de habla (o alimenta realtime)."""

    update_level = Signal(float)
    segment_ready = Signal(str, str)    # (path wav, carril) — "" en modo simple
    status_update = Signal(str)
    error_occurred = Signal(str)

    def __init__(self, source="loopback", device_index=None, realtime=None,
                 realtimes=None):
        super().__init__()
        self.source = source
        self.device_index = device_index
        self.realtime = realtime        # transcriptor streaming único, o None
        self.realtimes = realtimes or {}  # {carril: transcriptor} en modo dúo
        self.running = False
        self.samplerate = 48000
        self.channels = 2
        self.temp_dir = os.path.join(os.getcwd(), "temp_audio")
        os.makedirs(self.temp_dir, exist_ok=True)

    def _input_gen(self):
        """Generador de bloques del dispositivo de entrada (micro/VB-Cable)."""
        if self.source == "vbcable":
            idx = capture.find_device_by_name("cable output")
            if idx is None:
                raise RuntimeError("No se encontró el dispositivo VB-Cable.")
        else:
            idx = self.device_index
            if idx is None:
                idx = sd.query_devices(kind="input")["index"]

        # min(2, canales del dispositivo): los micros mono no admiten estéreo
        channels = min(2, sd.query_devices(idx)["max_input_channels"])

        def gen():
            stream = sd.InputStream(device=idx, channels=channels,
                                    samplerate=self.samplerate,
                                    blocksize=int(self.samplerate * 0.1))
            stream.start()
            try:
                while self.running:
                    yield stream.read(int(self.samplerate * 0.1))[0]
            finally:
                stream.stop()
                stream.close()
        return gen()

    def _lanes(self):
        """Devuelve [(nombre_carril, generador)] — separación por canal estilo
        Granola: loopback = «ellos», micro = «tú». Un solo carril sin nombre
        en las fuentes simples."""
        if self.source in ("loopback", "duo"):
            rec = capture.LoopbackRecorder(samplerate=self.samplerate,
                                           channels=self.channels, block_ms=100)
            them = rec.blocks(running=lambda: self.running)
            if self.source == "loopback":
                return [("", them)]
            # modo dúo: carril «ellos» (loopback) + carril «tú» (micro)
            return [("Entrevistador", them), ("Tú", self._input_gen())]
        return [("", self._input_gen())]

    def run(self):
        try:
            self.running = True
            self.status_update.emit("Capturando audio...")
            # commit_driven: gpt-live-transcribe no tiene VAD en servidor, así
            # que el segmentador local dispara input_audio_buffer.commit
            commit_driven = (self.realtime is not None
                             and getattr(self.realtime, "model", "") == "gpt-live-transcribe")
            need_vad = (self.realtime is None and not self.realtimes) or commit_driven

            lanes = self._lanes()
            segmenters = (
                {name: vad.VadSegmenter(sample_rate=vad.VAD_SAMPLE_RATE)
                 for name, _ in lanes}
                if need_vad else {}
            )

            while self.running:
                for name, gen in lanes:
                    try:
                        block = next(gen)
                    except StopIteration:
                        continue
                    if not self.running:
                        break
                    block = np.asarray(block, dtype=np.float32)
                    level = float(np.linalg.norm(block) / np.sqrt(max(len(block), 1)))
                    self.update_level.emit(level)

                    rt = self.realtimes.get(name) or self.realtime
                    if rt is not None:
                        rt.send_audio(
                            transcriber.pcm16_for_realtime(
                                block, self.samplerate, rt.sample_rate)
                        )
                        if commit_driven:
                            audio16 = vad.resample_linear(
                                block, self.samplerate, vad.VAD_SAMPLE_RATE)
                            for _ in segmenters[name].push(audio16):
                                rt.commit()
                    else:
                        audio16 = vad.resample_linear(
                            block, self.samplerate, vad.VAD_SAMPLE_RATE)
                        for segment in segmenters[name].push(audio16):
                            path = os.path.join(
                                self.temp_dir, f"seg_{uuid.uuid4().hex}.wav")
                            sf.write(path, segment, vad.VAD_SAMPLE_RATE)
                            self.segment_ready.emit(path, name)

            for name, segmenter in segmenters.items():
                for segment in segmenter.flush():
                    rt = self.realtimes.get(name) or self.realtime
                    if rt is not None:
                        rt.commit()
                    else:
                        path = os.path.join(
                            self.temp_dir, f"seg_{uuid.uuid4().hex}.wav")
                        sf.write(path, segment, vad.VAD_SAMPLE_RATE)
                        self.segment_ready.emit(path, name)

            self.status_update.emit("Captura detenida")
        except Exception as e:
            self.running = False
            self.error_occurred.emit(f"Error en captura: {e}")

    def stop(self):
        self.running = False


class AudioTranscriptionWorker(QThread):
    """Consume archivos de la cola y los transcribe con el proveedor elegido."""

    update_transcription = Signal(str, str)   # (texto, carril)
    status_update = Signal(str)
    error_occurred = Signal(str)

    def __init__(self, api_key, language_code,
                 provider=transcriber.DEFAULT_PROVIDER,
                 model=transcriber.DEFAULT_MODEL):
        super().__init__()
        self.api_key = api_key
        self.language_code = language_code
        self.provider = provider
        self.model = model
        self.running = False
        self.file_queue = queue.Queue()
        self.full_transcription = ""
        self.mutex = QMutex()

    def enqueue_file(self, filename, lane=""):
        self.file_queue.put((filename, lane))

    def run(self):
        self.running = True
        self.status_update.emit("Transcriptor listo")

        while self.running:
            try:
                try:
                    filename, lane = self.file_queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                self.status_update.emit(
                    f"Transcribiendo {os.path.basename(filename)} ({self.provider}/{self.model})"
                )
                try:
                    text = transcriber.transcribe_file(
                        self.api_key, filename, self.language_code,
                        provider=self.provider, model=self.model,
                    )
                    if text and not text.startswith("[Error"):
                        self.mutex.lock()
                        self.full_transcription = (
                            f"{self.full_transcription}\n{text}" if self.full_transcription else text
                        )
                        self.mutex.unlock()
                        self.update_transcription.emit(text, lane)  # fragmento nuevo
                        self.status_update.emit(f"+{len(text)} caracteres")
                except Exception as e:
                    self.error_occurred.emit(f"Error al transcribir: {e}")
                finally:
                    try:
                        os.remove(filename)
                    except OSError:
                        pass
            except Exception as e:
                self.error_occurred.emit(f"Error en el transcriptor: {e}")
                time.sleep(1)

        self.status_update.emit("Transcriptor detenido")

    def stop(self):
        self.running = False


# ---------------------------------------------------------------------------
# Ventana principal
# ---------------------------------------------------------------------------

class WhisperApp(QMainWindow):
    # Las señales se emiten desde hilos auxiliares y llegan encoladas a la UI.
    realtime_text = Signal(str, bool, str)   # (texto, final, carril)
    realtime_error = Signal(str)
    gate_fired = Signal(str, str, str)       # (texto, carril, modo)

    CAPTURE_SOURCES = [
        ("loopback", "Audio del sistema (loopback WASAPI)"),
        ("input", "Micrófono / dispositivo de entrada"),
        ("duo", "Loopback + micro (2 carriles: entrevistador / tú)"),
        ("vbcable", "VB-Cable (legacy)"),
    ]

    def __init__(self):
        super().__init__()
        self.settings = _load_settings()
        self.api_key = None
        self.current_audio_file = None
        self.recorder_thread = None
        self.transcription_thread = None
        self.selected_input_device = None
        self.capture_thread = None
        self.transcription_worker = None
        self.realtime = None
        self.realtimes = {}
        self.is_continuous_mode = False
        self._gpt_threads = []
        self._dying_threads = []
        self._ctx = deque()               # transcript completo «Carril: texto»
        self._draft_state = {}            # carril -> interim ya respondido
        self._gpt_streams = {}            # thread -> bloque GPT en streaming
        self._live_buffers = {}           # carril -> texto parcial acumulado
        self._gate_pending = set()        # carriles con gate clef en vuelo
        self._pinned = []                 # hechos fijados a mano, siempre en contexto

        self.init_ui()
        self.overlay = CompactOverlay()
        self.toast = AckToast()
        self.realtime_text.connect(self._append_transcript)
        self.realtime_error.connect(self.handle_continuous_error)
        self.gate_fired.connect(self._on_gate_fired)
        self._apply_settings()

        # Comandos por hook de teclado (eventos reales, sin falsos
        # positivos); el poll de 30 ms solo queda para los controles
        # mantenidos del panel (mover/opacidad).
        self._ll_hook = _install_ll_hook(self._dispatch_hotkey)
        self._hk_timer = QTimer(self)
        self._hk_timer.timeout.connect(self._overlay_keys_tick)
        self._hk_timer.start(30)

    # ------------------------- construcción de UI -------------------------

    def init_ui(self):
        self.setWindowTitle("audio_gpt — Transcriptor y asistente")
        self.setGeometry(100, 100, 980, 760)
        self.setStyleSheet(
            "QTextEdit, QPlainTextEdit { background: #ffffff; color: #1a1a1a;"
            "   border: 1px solid #c9c9c9; border-radius: 6px; }"
            "QLineEdit, QComboBox, QSpinBox { background: #ffffff;"
            "   color: #1a1a1a; }"
        )

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        self.continuous_button = QPushButton("INICIAR TRANSCRIPCIÓN CONTINUA")
        self.continuous_button.setMinimumHeight(56)
        self.continuous_button.setFont(QFont("Arial", 12, QFont.Bold))
        self._style_continuous_button(start=True)
        self.continuous_button.clicked.connect(self.toggle_continuous_mode)
        controls_layout = QHBoxLayout()
        controls_layout.addWidget(self.continuous_button, 1)
        self.config_toggle_button = QPushButton("Ocultar configuración")
        self.config_toggle_button.setCheckable(True)
        self.config_toggle_button.toggled.connect(self.toggle_configuration)
        controls_layout.addWidget(self.config_toggle_button)
        main_layout.addLayout(controls_layout)

        splitter = self.main_splitter = QSplitter(Qt.Vertical)
        main_layout.addWidget(splitter, 1)

        top = self.config_panel = QWidget()
        top_layout = QVBoxLayout(top)

        # --- captura ---
        rec_group = QGroupBox("Captura de audio")
        rec_layout = QVBoxLayout(rec_group)

        source_layout = QHBoxLayout()
        source_layout.addWidget(QLabel("Fuente:"))
        self.source_selector = QComboBox()
        for key, label in self.CAPTURE_SOURCES:
            self.source_selector.addItem(label, key)
        self.audio_setup_button = QPushButton("Dispositivos…")
        self.audio_setup_button.clicked.connect(self.show_audio_setup)
        source_layout.addWidget(self.source_selector, 1)
        source_layout.addWidget(self.audio_setup_button)
        rec_layout.addLayout(source_layout)

        duration_layout = QHBoxLayout()
        duration_layout.addWidget(QLabel("Grabación manual (s):"))
        self.duration_input = QSpinBox()
        self.duration_input.setRange(1, 300)
        self.duration_input.setValue(30)
        duration_layout.addWidget(self.duration_input)
        self.record_button = QPushButton("Grabar")
        self.play_button = QPushButton("Reproducir")
        self.play_button.setEnabled(False)
        self.record_button.clicked.connect(self.start_recording)
        self.play_button.clicked.connect(self.play_audio)
        duration_layout.addWidget(self.record_button)
        duration_layout.addWidget(self.play_button)
        duration_layout.addStretch()
        rec_layout.addLayout(duration_layout)

        self.level_monitor = AudioLevelWidget()
        rec_layout.addWidget(self.level_monitor)
        self.progress_bar = QProgressBar()
        rec_layout.addWidget(self.progress_bar)
        top_layout.addWidget(rec_group)

        # --- transcripción ---
        tr_group = QGroupBox("Transcripción")
        tr_layout = QVBoxLayout(tr_group)

        prov_layout = QHBoxLayout()
        prov_layout.addWidget(QLabel("Proveedor:"))
        self.provider_selector = QComboBox()
        for key, meta in transcriber.PROVIDERS.items():
            self.provider_selector.addItem(meta["label"], key)
        self.provider_selector.currentIndexChanged.connect(self._on_provider_changed)
        prov_layout.addWidget(self.provider_selector, 1)
        self.change_api_button = QPushButton("API key…")
        self.change_api_button.clicked.connect(self.change_api_key)
        prov_layout.addWidget(self.change_api_button)
        tr_layout.addLayout(prov_layout)

        model_layout = QHBoxLayout()
        model_layout.addWidget(QLabel("Modelo:"))
        self.model_selector = QComboBox()
        model_layout.addWidget(self.model_selector, 1)
        model_layout.addWidget(QLabel("Idioma:"))
        self.language_selector = QComboBox()
        for code, name in WhisperService.get_available_languages().items():
            self.language_selector.addItem(name, code)
        self.language_selector.setCurrentIndex(1)  # Español por defecto
        model_layout.addWidget(self.language_selector, 1)
        tr_layout.addLayout(model_layout)

        self.provider_help = QLabel("")
        self.provider_help.setWordWrap(True)
        self.provider_help.setStyleSheet("color: #888; font-size: 11px;")
        tr_layout.addWidget(self.provider_help)

        manual_layout = QHBoxLayout()
        self.transcribe_button = QPushButton("Transcribir grabación")
        self.transcribe_button.setEnabled(False)
        self.transcribe_button.clicked.connect(self.transcribe_audio)
        manual_layout.addWidget(self.transcribe_button)
        self.auto_gpt_checkbox = QCheckBox("Responder con GPT automáticamente")
        manual_layout.addWidget(self.auto_gpt_checkbox)
        self.manual_gpt_checkbox = QCheckBox("GPT solo bajo demanda")
        self.manual_gpt_checkbox.setToolTip(
            "Nada se envía a GPT salvo orden tuya: botón «Enviar a GPT», "
            "Ctrl+Q o Alt+G. Con esto activo el modo automático queda "
            "apagado y deshabilitado.")
        self.manual_gpt_checkbox.toggled.connect(self._on_manual_only_changed)
        manual_layout.addWidget(self.manual_gpt_checkbox)
        self.diarize_checkbox = QCheckBox("Diarizar (panel)")
        self.diarize_checkbox.setToolTip(
            "Deepgram nova-3: etiqueta voces distintas dentro del mismo carril "
            "(<S0>, <S1>…). Para llamadas con varios entrevistadores."
        )
        manual_layout.addWidget(self.diarize_checkbox)
        manual_layout.addStretch()
        tr_layout.addLayout(manual_layout)

        keyterm_layout = QHBoxLayout()
        keyterm_layout.addWidget(QLabel("Términos clave:"))
        self.keyterms_input = QLineEdit()
        self.keyterms_input.setPlaceholderText(
            "python, kubernetes, pytorch… (Deepgram nova-3 los escucha mejor)")
        keyterm_layout.addWidget(self.keyterms_input, 1)
        tr_layout.addLayout(keyterm_layout)

        brief_layout = QHBoxLayout()
        brief_layout.addWidget(QLabel("Brief de la entrevista:"))
        self.brief_input = QPlainTextEdit()
        self.brief_input.setMaximumHeight(56)
        self.brief_input.setPlaceholderText(
            "Puesto, empresa, tu experiencia aprobada, límites "
            "(p. ej. «sin Kubernetes en prod»). Va en cada llamada a GPT — "
            "las respuestas se adaptan a este contexto.")
        brief_layout.addWidget(self.brief_input, 1)
        tr_layout.addLayout(brief_layout)

        top_layout.addWidget(tr_group)
        splitter.addWidget(top)

        # --- salidas ---
        bottom = QWidget()
        bottom_layout = self.output_layout = QVBoxLayout(bottom)

        out_group = QGroupBox("Transcripción")
        out_layout = QVBoxLayout(out_group)

        lanes_row = QHBoxLayout()

        def _lane_pane(title, color, placeholder):
            col = QVBoxLayout()
            lab = QLabel(title)
            lab.setStyleSheet(f"color: {color}; font-weight: bold;")
            col.addWidget(lab)
            edit = QTextEdit()
            edit.setReadOnly(True)
            edit.setPlaceholderText(placeholder)
            col.addWidget(edit)
            live = QLabel("En directo: \u2014")
            live.setWordWrap(True)
            live.setStyleSheet("color: #555; font-style: italic;")
            col.addWidget(live)
            return col, edit, live

        col_int, self.interviewer_output, self.interviewer_live = _lane_pane(
            "Entrevistador", "#1a56db", "Voces de la llamada…")
        col_you, self.you_output, self.you_live = _lane_pane(
            "Tú (micro)", "#0f7a3d", "Tu voz…")
        lanes_row.addLayout(col_int)
        lanes_row.addLayout(col_you)
        out_layout.addLayout(lanes_row)

        text_buttons = QHBoxLayout()
        self.copy_button = QPushButton("Copiar")
        self.save_button = QPushButton("Guardar")
        self.clear_button = QPushButton("Limpiar")
        self.pin_button = QPushButton("Fijar")
        self.pin_button.setToolTip(
            "Fija la selección del transcript: requisitos, cifras, "
            "decisiones — van en el contexto de cada llamada y nunca ruedan.")
        self.copy_button.clicked.connect(self.copy_text)
        self.save_button.clicked.connect(self.save_text)
        self.clear_button.clicked.connect(self.clear_text)
        self.pin_button.clicked.connect(self.pin_selection)
        hk_hint = QLabel("Ctrl+I panel oculto · Alt+G / Ctrl+Q enviar a GPT")
        hk_hint.setStyleSheet("color: #888; font-size: 11px;")
        self.send_to_gpt_button = QPushButton("Enviar a GPT")
        self.send_to_gpt_button.setStyleSheet(
            "QPushButton { background-color: #6a0dad; color: white; }"
            "QPushButton:hover { background-color: #8a2be2; }"
        )
        self.send_to_gpt_button.setMinimumHeight(30)
        self.send_to_gpt_button.clicked.connect(self.send_to_gpt)
        text_buttons.addWidget(self.copy_button)
        text_buttons.addWidget(self.save_button)
        text_buttons.addWidget(self.clear_button)
        text_buttons.addWidget(self.pin_button)
        text_buttons.addWidget(hk_hint)
        text_buttons.addStretch()
        self.gpt_engine_combo = QComboBox()
        self.gpt_engine_combo.addItem("API OpenAI", "openai")
        self.gpt_engine_combo.addItem("Cloudflare AI (créditos CF)", "cloudflare")
        self.gpt_engine_combo.addItem("Codex CLI (ChatGPT sub)", "codex")
        self.gpt_engine_combo.setToolTip(
            "API OpenAI: pago por uso (necesita API key). "
            "Cloudflare: openai/gpt-6-luna servido por Workers AI "
            "(necesita CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID). "
            "Codex CLI: usa tu suscripción ChatGPT (necesita codex instalado)."
        )
        text_buttons.addWidget(self.gpt_engine_combo)
        text_buttons.addWidget(self.send_to_gpt_button)
        out_layout.addLayout(text_buttons)
        bottom_layout.addWidget(out_group)

        gpt_group = QGroupBox("Respuestas GPT")
        gpt_layout = QVBoxLayout(gpt_group)
        self.gpt_output = QTextEdit()
        self.gpt_output.setReadOnly(True)
        self.gpt_output.setPlaceholderText("Respuestas automáticas de GPT…")
        gpt_layout.addWidget(self.gpt_output)
        bottom_layout.addWidget(gpt_group)

        splitter.addWidget(bottom)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Listo")

        self._on_provider_changed()

    def toggle_configuration(self, hidden):
        if hidden:
            self._expanded_splitter_sizes = self.main_splitter.sizes()
        self.config_panel.setVisible(not hidden)
        self.output_layout.setStretch(1, 1 if hidden else 0)
        self.config_toggle_button.setText(
            "Mostrar configuración" if hidden else "Ocultar configuración")
        if not hidden:
            self.main_splitter.setSizes(self._expanded_splitter_sizes)

    def _style_continuous_button(self, start):
        color, hover, pressed = (
            ("#4CAF50", "#45a049", "#398438") if start else ("#f44336", "#e53935", "#c62828")
        )
        self.continuous_button.setStyleSheet(
            f"QPushButton {{ background-color: {color}; color: white; border-radius: 8px; }}"
            f"QPushButton:hover {{ background-color: {hover}; }}"
            f"QPushButton:pressed {{ background-color: {pressed}; }}"
        )

    def _apply_settings(self):
        s = self.settings
        provider = s.get("provider")
        if provider in transcriber.PROVIDERS:
            self.provider_selector.setCurrentIndex(
                list(transcriber.PROVIDERS).index(provider))
        self._on_provider_changed()
        model = s.get("model")
        if model:
            idx = self.model_selector.findData(model)
            if idx >= 0:
                self.model_selector.setCurrentIndex(idx)
        lang = s.get("language")
        if lang is not None:
            idx = self.language_selector.findData(lang)
            if idx >= 0:
                self.language_selector.setCurrentIndex(idx)
        source = s.get("source")
        if source:
            idx = self.source_selector.findData(source)
            if idx >= 0:
                self.source_selector.setCurrentIndex(idx)
        engine = s.get("gpt_engine")
        if engine:
            idx = self.gpt_engine_combo.findData(engine)
            if idx >= 0:
                self.gpt_engine_combo.setCurrentIndex(idx)
        self.keyterms_input.setText(s.get("dg_keyterms", ""))
        self.diarize_checkbox.setChecked(bool(s.get("dg_diarize")))
        self.brief_input.setPlainText(s.get("interview_brief", ""))
        # Por defecto envío a GPT SOLO manual: el usuario decide cuándo.
        self.manual_gpt_checkbox.setChecked(s.get("manual_gpt_only", True))

    def _persist_settings(self):
        _save_settings({
            "provider": self.provider_selector.currentData(),
            "model": self.model_selector.currentData(),
            "language": self.language_selector.currentData(),
            "source": self.source_selector.currentData(),
            "gpt_engine": self.gpt_engine_combo.currentData(),
            "dg_keyterms": self.keyterms_input.text().strip(),
            "dg_diarize": self.diarize_checkbox.isChecked(),
            "interview_brief": self.brief_input.toPlainText().strip(),
            "manual_gpt_only": self.manual_gpt_checkbox.isChecked(),
        })

    def _on_manual_only_changed(self, on):
        """Modo manual: el envío a GPT solo ocurre bajo orden explícita."""
        self.auto_gpt_checkbox.setEnabled(not on)
        if on:
            self.auto_gpt_checkbox.setChecked(False)

    # ------------------------- proveedor / api key -------------------------

    def _on_provider_changed(self):
        provider = self.provider_selector.currentData()
        meta = transcriber.PROVIDERS.get(provider, {})
        self.model_selector.clear()
        for value, label in meta.get("models", []):
            self.model_selector.addItem(label, value)
        self.provider_help.setText(meta.get("help", ""))
        self.api_key = ApiKeyManager.load_api_key(provider)
        needs_key = meta.get("key_env") is not None
        self.change_api_button.setEnabled(needs_key)
        if needs_key and not self.api_key and hasattr(self, 'status_bar'):
            self.status_bar.showMessage(
                f"{meta.get('label', provider)} necesita una API key "
                "(bot\u00f3n \u00abAPI key\u2026\u00bb)")

    def change_api_key(self):
        provider = self.provider_selector.currentData()
        dialog = ApiKeyDialog(self, provider)
        if dialog.exec() == QDialog.Accepted:
            self.api_key = dialog.get_api_key()
            self.status_bar.showMessage("API key actualizada")

    def _current_provider(self):
        return self.provider_selector.currentData()

    def _current_model(self):
        return self.model_selector.currentData()

    def _current_language(self):
        return self.language_selector.currentData()

    def _current_source(self):
        return self.source_selector.currentData()

    # ------------------------- grabación manual -------------------------

    def start_recording(self):
        self.record_button.setEnabled(False)
        self.transcribe_button.setEnabled(False)
        self.play_button.setEnabled(False)

        self.current_audio_file = os.path.join(os.getcwd(), "recording.wav")
        self.recorder_thread = AudioRecorderThread(
            self.current_audio_file,
            self.duration_input.value(),
            source=self._current_source(),
            device_index=self.selected_input_device,
        )
        self.recorder_thread.update_progress.connect(self.progress_bar.setValue)
        self.recorder_thread.update_level.connect(self.level_monitor.set_level)
        self.recorder_thread.recording_complete.connect(self.recording_finished)
        self.progress_bar.setValue(0)
        self.status_bar.showMessage("Grabando…")
        self.recorder_thread.start()

    def recording_finished(self, success, message):
        self.record_button.setEnabled(True)
        if success:
            self.status_bar.showMessage("Grabación completada")
            self.play_button.setEnabled(True)
            self.transcribe_button.setEnabled(True)
        else:
            self.status_bar.showMessage(f"Error: {message}")
            QMessageBox.critical(self, "Error", f"Error durante la grabación: {message}")

    def play_audio(self):
        if self.current_audio_file and os.path.exists(self.current_audio_file):
            data, sr = sf.read(self.current_audio_file)
            sd.play(data, sr)
        else:
            QMessageBox.warning(self, "Error", "No hay audio para reproducir")

    def show_audio_setup(self):
        dialog = AudioDeviceSetupDialog(self)
        if dialog.exec() == QDialog.Accepted:
            selected = dialog.get_selected_devices()
            self.selected_input_device = selected["input"]
            self.status_bar.showMessage("Dispositivo de entrada actualizado")

    def transcribe_audio(self):
        if not self.current_audio_file or not os.path.exists(self.current_audio_file):
            QMessageBox.warning(self, "Error", "No hay audio grabado para transcribir")
            return
        self.transcribe_button.setEnabled(False)
        self.status_bar.showMessage("Transcribiendo…")
        self.transcription_thread = TranscriptionThread(
            self.api_key, self.current_audio_file, self._current_language(),
            provider=self._current_provider(), model=self._current_model(),
        )
        self.transcription_thread.transcription_complete.connect(self.transcription_finished)
        self.transcription_thread.start()

    def transcription_finished(self, success, result):
        self.transcribe_button.setEnabled(True)
        if success:
            self._clear_transcript_views()
            self._lane_widget("").setPlainText(result)

            self.status_bar.showMessage("Transcripción completada")
        else:
            QMessageBox.critical(self, "Error", f"Error en la transcripción: {result}")
            self.status_bar.showMessage(f"Error: {result}")

    # ------------------------- modo continuo -------------------------

    def toggle_continuous_mode(self):
        if not self.is_continuous_mode:
            self.start_continuous_mode()
        else:
            self.stop_continuous_mode()

    def start_continuous_mode(self):
        provider = self._current_provider()
        meta = transcriber.PROVIDERS.get(provider, {})
        if meta.get("key_env") and not self.api_key:
            QMessageBox.warning(self, "Error", "No hay API key configurada para este proveedor")
            return
        if provider == "local" and not self._check_local_available():
            return
        if provider != "openai-realtime" and not vad.is_available():
            QMessageBox.warning(
                self, "Error",
                "webrtcvad no está instalado (pip install webrtcvad-wheels)."
            )
            return

        source = self._current_source()
        if source == "duo" and provider == "openai-realtime":
            QMessageBox.warning(
                self, "Error",
                "El modo dúo (2 carriles) aún no está soportado con OpenAI "
                "Realtime. Usa OpenAI, Groq, Local o Deepgram."
            )
            return
        if source in ("loopback", "duo") and not capture.loopback_available():
            QMessageBox.warning(
                self, "Error",
                "El loopback WASAPI no está disponible. Prueba con micrófono o VB-Cable."
            )
            return

        self._set_controls_enabled(False)
        self.continuous_button.setText("DETENER TRANSCRIPCIÓN CONTINUA")
        self._style_continuous_button(start=False)
        self._clear_transcript_views()
        self._persist_settings()

        language = self._current_language()
        model = self._current_model()

        if meta.get("streaming") or provider == "openai-realtime":
            if not self._start_realtime(language, model):
                self._set_controls_enabled(True)
                self.continuous_button.setText("INICIAR TRANSCRIPCIÓN CONTINUA")
                self._style_continuous_button(start=True)
                return
        else:
            self.capture_thread = ContinuousCaptureThread(
                source=source, device_index=self.selected_input_device)
            self.capture_thread.update_level.connect(self.level_monitor.set_level)
            self.capture_thread.status_update.connect(self.status_bar.showMessage)
            self.capture_thread.error_occurred.connect(self.handle_continuous_error)

            self.transcription_worker = AudioTranscriptionWorker(
                self.api_key, language, provider=provider, model=model)
            self.transcription_worker.update_transcription.connect(self._on_new_segment)
            self.transcription_worker.status_update.connect(self.status_bar.showMessage)
            self.transcription_worker.error_occurred.connect(self.handle_continuous_error)
            self.capture_thread.segment_ready.connect(self.transcription_worker.enqueue_file)

            self.transcription_worker.start()
            self.capture_thread.start()

        self.is_continuous_mode = True
        self.overlay.set_listening(True)
        self.status_bar.showMessage("Transcripción continua iniciada")

    def _start_realtime(self, language, model):
        """Crea el/los transcriptor(es) streaming según proveedor y fuente."""
        provider = self._current_provider()
        source = self._current_source()

        keyterms = [t.strip() for t in self.keyterms_input.text().split(",")
                    if t.strip()]
        diarize = (self.diarize_checkbox.isChecked()
                   and provider == "deepgram"
                   and model.startswith("nova"))

        def make_rt(lane=""):
            cb = (lambda t, fin, l=lane: self.realtime_text.emit(t, fin, l))
            err = (lambda m: self.realtime_error.emit(m))
            if provider == "deepgram":
                lane_model = model
                lane_diarize = diarize
                if source == "duo":
                    if lane == "T\u00fa":
                        lane_model = "flux-general-multi"
                        lane_diarize = False
                    elif lane != "Entrevistador":
                        lane_diarize = False
                return transcriber.DeepgramRealtime(
                    self.api_key, model=lane_model, language=language,
                    on_transcript=cb, on_error=err,
                    diarize=lane_diarize, keyterms=keyterms)
            return transcriber.RealtimeTranscriber(
                self.api_key, model=model, language=language,
                prompt=transcriber.DEFAULT_PROMPT,
                on_transcript=cb, on_error=err)

        lanes = ["Entrevistador", "Tú"] if source == "duo" else [""]
        self.realtimes = {}
        try:
            for lane_label in lanes:
                rt = make_rt(lane_label)
                rt.start()
                self.realtimes[lane_label] = rt
        except Exception as e:
            for rt in self.realtimes.values():
                try:
                    rt.stop()
                except Exception:
                    pass
            self.realtimes = {}
            QMessageBox.critical(self, "Error", f"No se pudo abrir la sesión streaming: {e}")
            return False

        if source == "duo":
            self.capture_thread = ContinuousCaptureThread(
                source=source, device_index=self.selected_input_device,
                realtimes=self.realtimes)
        else:
            self.realtime = next(iter(self.realtimes.values()))
            self.capture_thread = ContinuousCaptureThread(
                source=source, device_index=self.selected_input_device,
                realtime=self.realtime)
        self.capture_thread.update_level.connect(self.level_monitor.set_level)
        self.capture_thread.status_update.connect(self.status_bar.showMessage)
        self.capture_thread.error_occurred.connect(self.handle_continuous_error)
        self.capture_thread.start()
        return True

    # ------------------------- hotkeys globales / overlay ------------------

    def _dispatch_hotkey(self, combo):
        getattr(self, "_hk_" + HOTKEYS[combo][2])()

    def _overlay_keys_tick(self):
        """Ctrl+flechas = mover panel · Ctrl+± = opacidad. Órdenes dirigidas
        al panel: solo actúan cuando está visible."""
        if sys.platform != "win32" or not self.overlay.isVisible():
            return
        u32 = ctypes.windll.user32
        if (not u32.GetAsyncKeyState(0x11) & 0x8000
                or u32.GetAsyncKeyState(0x12) & 0x8000):
            return
        for vk, (dx, dy) in _ARROW_VK.items():
            if u32.GetAsyncKeyState(vk) & 0x8000:
                self.overlay.nudge(
                    dx * _OVERLAY_MOVE_STEP, dy * _OVERLAY_MOVE_STEP)
        for vk in _PLUS_VK:
            if u32.GetAsyncKeyState(vk) & 0x8000:
                self.overlay.adjust_opacity(_OVERLAY_OPACITY_STEP)
                break
        for vk in _MINUS_VK:
            if u32.GetAsyncKeyState(vk) & 0x8000:
                self.overlay.adjust_opacity(-_OVERLAY_OPACITY_STEP)
                break

    def _ack(self, text):
        """Constancia de que el comando entró: flash en el panel si está
        visible; si está oculto, un toast flotante; siempre en status bar."""
        self.status_bar.showMessage(text)
        if self.overlay.isVisible():
            self.overlay.ack(text)
        else:
            self.toast.show_ack(text)

    def _hk_answer_last(self):
        """Rescate: responder la última intervención del entrevistador aunque
        el gate no la haya pillado."""
        self._ack("Ctrl+Q · responder última intervención")
        last = next((x for x in reversed(self._ctx)
                     if not x.startswith("Tú:")), None)
        if not last:
            self._ack("Ctrl+Q · nada que responder todavía")
            return
        text = last.split(":", 1)[1].strip() if ":" in last else last
        self._fire_gpt(text, "Entrevistador", kind="Manual",
                       effort=self._review_effort("medium"))

    def _hk_toggle_compact(self):
        """Ctrl+I — ÚNICO control de visibilidad del panel."""
        if self.overlay.isVisible():
            self.overlay.ack("Ctrl+I · ocultando panel…")
            QTimer.singleShot(450, self.overlay.hide)
        else:
            self.overlay.set_listening(self.is_continuous_mode)
            self.overlay.show()
            self.overlay.raise_()
            self.overlay.ack("Ctrl+I · panel visible")

    def _hk_toggle_auto(self):
        if self.manual_gpt_checkbox.isChecked():
            self._ack("Ctrl+M · auto-GPT bloqueado (modo manual activo)")
            return
        self.auto_gpt_checkbox.toggle()
        self._ack(
            f"Ctrl+M · auto-GPT {'ON' if self.auto_gpt_checkbox.isChecked() else 'OFF'}")

    def _hk_send_gpt(self):
        self._ack("Alt+G · enviando a GPT…")
        self.send_to_gpt()

    def _hk_toggle_capture(self):
        self._ack("Alt+T · iniciando…" if not self.is_continuous_mode
                  else "Alt+T · deteniendo…")
        self.toggle_continuous_mode()

    @Slot(str, bool, str)
    def _append_transcript(self, text, is_final, lane=""):
        lane = self._lane_key(lane)
        if is_final:
            self._live_buffers.pop(lane, None)
            self._set_live_transcript(lane, "")
            self.overlay.set_live(f"{lane}: {text}")
            self._draft_state.pop(lane, None)   # turno cerrado: próximo borrador
            self._on_new_segment(text, lane)
            return

        if self._current_provider() == "openai-realtime":
            text = self._live_buffers.get(lane, "") + text
        self._live_buffers[lane] = text
        self._set_live_transcript(lane, text)
        self.overlay.set_live(f"{lane}: {text}")
        # Interim: borrador anticipado mientras la persona sigue hablando.
        if lane == "T\u00fa" or not self.auto_gpt_checkbox.isChecked():
            return
        prev = self._draft_state.get(lane)
        if prev is not None and prev in text:
            return                                # mismo turno, ya disparado
        self._gate_async(text, lane, "Borrador")

    # ------------------------- gate clef-flash ----------------------------

    def _gate_async(self, text, lane, kind):
        """Decide el disparo en hilo: clef-flash con creds CF, heurística
        local como fallback. _gate_pending evita llamadas por interim."""
        if lane in self._gate_pending:
            return
        self._gate_pending.add(lane)

        def run():
            try:
                verdict = clef_question(text)
                ok = verdict[0] if verdict else looks_like_question(text)
                if ok:
                    self.gate_fired.emit(text, lane, kind)
            finally:
                self._gate_pending.discard(lane)

        threading.Thread(target=run, daemon=True).start()

    @Slot(str, str, str)
    def _on_gate_fired(self, text, lane, kind):
        if kind == "Borrador":
            self._draft_state[lane] = text
            self._fire_gpt(text, lane, kind=kind)
        else:  # Revisión
            self._fire_gpt(text, lane, kind=kind,
                           effort=self._review_effort("medium"))

    def _retire_thread(self, thread):
        """Detiene un QThread sin destruirlo mientras siga corriendo.

        wait() con cota; si no termina, se retiene la referencia hasta su
        señal finished para que Qt no lo destruya en ejecución (crash).
        """
        if thread is None:
            return
        thread.stop()
        if not thread.wait(3000):
            self._dying_threads.append(thread)
            thread.finished.connect(
                lambda t=thread: self._dying_threads.remove(t))

    def stop_continuous_mode(self):
        self._retire_thread(self.capture_thread)
        self.capture_thread = None
        for rt in self.realtimes.values():
            try:
                rt.stop()
            except Exception:
                pass
        self.realtimes = {}
        self.realtime = None
        self._retire_thread(self.transcription_worker)
        self.transcription_worker = None

        self._set_controls_enabled(True)
        self.continuous_button.setText("INICIAR TRANSCRIPCIÓN CONTINUA")
        self._style_continuous_button(start=True)
        self.is_continuous_mode = False
        self.overlay.set_listening(False)
        self.status_bar.showMessage("Transcripción continua detenida")

    def _set_controls_enabled(self, enabled):
        for w in (self.record_button, self.play_button, self.transcribe_button,
                  self.audio_setup_button, self.source_selector,
                  self.language_selector, self.duration_input,
                  self.provider_selector, self.model_selector):
            w.setEnabled(enabled)

    def _check_local_available(self):
        try:
            __import__("faster_whisper")
            return True
        except ImportError:
            QMessageBox.warning(
                self, "faster-whisper no instalado",
                "Para transcripción local instala: pip install faster-whisper\n"
                "(descargará el modelo la primera vez, ~1.6 GB para large-v3-turbo)."
            )
            return False

    def _lane_key(self, lane=""):
        """Canal visible: loopback a la izquierda, micrófono a la derecha."""
        if lane in ("Entrevistador", "T\u00fa"):
            return lane
        return "T\u00fa" if self._current_source() == "input" else "Entrevistador"

    def _lane_widget(self, lane=""):
        if self._lane_key(lane) == "T\u00fa":
            return self.you_output
        return self.interviewer_output

    def _set_live_transcript(self, lane, text):
        label = (self.you_live if self._lane_key(lane) == "T\u00fa"
                 else self.interviewer_live)
        text = text.strip()
        label.setText(f"En directo: {text[-500:]}" if text else "En directo: \u2014")

    def _clear_transcript_views(self):
        self.interviewer_output.clear()
        self.you_output.clear()
        self._live_buffers.clear()
        self.interviewer_live.setText("En directo: \u2014")
        self.you_live.setText("En directo: \u2014")

    def _on_new_segment(self, text, lane=""):
        """Llamado con cada fragmento nuevo transcrito (modo continuo)."""
        lane = self._lane_key(lane)
        display = f"{lane}: {text}"
        if lane == "Tú":
            self.you_output.append(_format_transcript_html(text))
        else:
            self.interviewer_output.append(_format_transcript_html(text))
        self.overlay.add_transcript(lane, text)

        if not text.strip():
            return
        if self.auto_gpt_checkbox.isChecked() and lane != "Tú":
            # Revisión sobre el final: mismo gate, esfuerzo mayor.
            self._gate_async(display, lane, "Revisión")
        self._ctx.append(display)

    def _review_effort(self, base):
        """Revisión al menos a «medium» salvo que el usuario pida más."""
        order = ("none", "minimal", "low", "medium", "high", "max")
        cfg = (GptClient.load_config() or {}).get("reasoning_effort") or "low"
        try:
            return base if order.index(base) > order.index(cfg) else cfg
        except ValueError:
            return base

    def _build_context(self):
        """Contexto = transcript COMPLETO de la llamada + hechos fijados.
        Con ~8k tokens a los 45 min el input cuesta <$0.001 y ~0.3s de
        prefill — nada rueda, no hace falta ledger ni recorte."""
        parts = []
        if self._pinned:
            parts.append("Hechos fijados:\n" + "\n".join(self._pinned))
        convo = list(self._ctx)
        if convo:
            parts.append("Conversación completa:\n" + "\n".join(convo))
        return "\n\n".join(parts)

    def _fire_gpt(self, text, lane, kind, effort=None):
        """Lanza una consulta GPT (borrador o revisión) con contexto."""
        engine = self.gpt_engine_combo.currentData()
        context = self._build_context()
        gpt_input = f"{lane}: {text}" if lane and not text.startswith(lane) else text
        if effort == "medium":
            effort = self._review_effort("medium")
        # Borradores: boceto corto y rápido (~3s en CF a ~55 tok/s);
        # revisiones: respuesta completa.
        max_tokens = 200 if kind == "Borrador" else None

        brief = self.brief_input.toPlainText().strip()
        if engine == "codex":
            thread = CodexCliThread(gpt_input, context=context, brief=brief)
        elif engine == "cloudflare":
            thread = GptQueryThread(
                None, gpt_input, engine="cloudflare",
                context=context, effort=effort, max_tokens=max_tokens,
                brief=brief, allow_web=(kind != "Borrador"),
                fast=(kind == "Borrador"))
        else:
            gpt_key = ApiKeyManager.load_api_key("openai")
            if not gpt_key:
                self.status_bar.showMessage(
                    "Auto-GPT necesita una API key de OpenAI (botón «API key…»)"
                )
                return
            thread = GptQueryThread(
                gpt_key, gpt_input, context=context, effort=effort,
                max_tokens=max_tokens, brief=brief,
                allow_web=(kind != "Borrador"),
                fast=(kind == "Borrador"))

        header = f"[{kind}] {gpt_input}" if kind else gpt_input
        self._begin_gpt_stream(thread, header)
        self.overlay.gpt_update(thread, "…", header=header)
        self._gpt_threads.append(thread)
        if hasattr(thread, "query_delta"):
            thread.query_delta.connect(
                lambda txt, t=thread: self._update_gpt_stream(t, txt))
        thread.query_complete.connect(
            lambda ok, res, h=header, t=thread:
            self._on_auto_gpt_result(ok, res, h, t))
        thread.query_complete.connect(
            lambda *a, t=thread: self._gpt_threads.remove(t))
        thread.start()

    def _begin_gpt_stream(self, thread, header):
        """Reserva un bloque editable al final para la respuesta parcial."""
        cursor = self.gpt_output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if self.gpt_output.toPlainText().strip():
            cursor.insertText("\n\n")
        start = cursor.position()
        cursor.insertText(f"{header}\n")
        self._gpt_streams[thread] = {
            "start": start, "end": cursor.position(), "header": header}

    def _update_gpt_stream(self, thread, text):
        entry = self._gpt_streams.get(thread)
        if entry is None:
            return
        cursor = self.gpt_output.textCursor()
        cursor.setPosition(entry["start"])
        cursor.setPosition(entry["end"], QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        cursor.insertText(f'{entry["header"]}\n{text}')
        new_end = cursor.position()
        shift = new_end - entry["end"]
        entry["end"] = new_end
        if shift:
            for other, other_entry in self._gpt_streams.items():
                if other is not thread and other_entry["start"] > entry["start"]:
                    other_entry["start"] += shift
                    other_entry["end"] += shift
        bar = self.gpt_output.verticalScrollBar()
        bar.setValue(bar.maximum())
        self.overlay.gpt_update(thread, text)

    def _finish_gpt_stream(self, thread, text):
        self._update_gpt_stream(thread, text)
        self._gpt_streams.pop(thread, None)

    def _on_auto_gpt_result(self, success, result, header="", thread=None):
        if success:
            block = f"{header}\n{result}" if header else result
            if thread in self._gpt_streams:
                self._finish_gpt_stream(thread, result)
            else:
                self.gpt_output.append(block)
                self.gpt_output.append("")
            self.overlay.gpt_update(thread, result)
            self.status_bar.showMessage("Respuesta GPT recibida")
        else:
            if thread in self._gpt_streams:
                self._finish_gpt_stream(thread, f"Error: {result}")
            self.overlay.gpt_update(thread, f"Error: {result}")
            self.status_bar.showMessage(f"GPT: {result}")

    def handle_continuous_error(self, error_msg):
        if not self.is_continuous_mode:
            return  # coalesce: errores en vuelo tras la primera parada
        self.stop_continuous_mode()       # detener antes de abrir el modal
        QMessageBox.warning(self, "Error", error_msg)

    # ------------------------- texto / gpt manual -------------------------

    def _transcript_text(self):
        """Ambos carriles en texto plano, con cabecera de carril."""
        parts = []
        it = self.interviewer_output.toPlainText().strip()
        you = self.you_output.toPlainText().strip()
        if it:
            parts.append("Entrevistador:\n" + it)
        if you:
            parts.append("Tú:\n" + you)
        return "\n\n".join(parts)

    def copy_text(self):
        text = self._transcript_text()
        if text:
            QApplication.clipboard().setText(text)
            self.status_bar.showMessage("Copiado al portapapeles")

    def save_text(self):
        text = self._transcript_text()
        if not text:
            QMessageBox.warning(self, "Advertencia", "No hay texto para guardar")
            return
        # Default sensato: transcripts/transcripcion_YYYYmmdd_HHMMSS.txt
        os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)
        default = os.path.join(
            TRANSCRIPTS_DIR,
            "transcripcion_" + time.strftime("%Y%m%d_%H%M%S") + ".txt")
        filename, _ = QFileDialog.getSaveFileName(
            self, "Guardar transcripción", default,
            "Archivos de texto (*.txt);;Todos los archivos (*)")
        if filename:
            try:
                with open(filename, "w", encoding="utf-8") as f:
                    f.write(text)
                self.status_bar.showMessage(f"Guardado en {filename}")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Error al guardar: {e}")

    def pin_selection(self):
        """Fija el texto seleccionado del transcript: va en cada llamada."""
        sel = self.interviewer_output.textCursor().selectedText()
        if not sel.strip():
            sel = self.you_output.textCursor().selectedText()
        sel = sel.replace("\u2029", "\n").strip()
        if not sel:
            self.status_bar.showMessage("Selecciona texto en la transcripción para fijarlo")
            return
        self._pinned.append(sel)
        self.status_bar.showMessage(
            f"Fijado ({len(self._pinned)} hecho(s) en contexto): {sel[:60]}")

    def clear_text(self):
        self._clear_transcript_views()
        self.gpt_output.clear()
        self._ctx.clear()            # estado oculto no sobrevive al «Limpiar»
        self._draft_state.clear()
        self._pinned.clear()
        self.overlay.clear_log()
        self.status_bar.showMessage("Transcripción y contexto borrados")

    def send_to_gpt(self):
        transcription = self._transcript_text()
        if not transcription:
            self.status_bar.showMessage("No hay texto para enviar a GPT")
            return

        engine = self.gpt_engine_combo.currentData()
        brief = self.brief_input.toPlainText().strip()
        context = self._build_context()
        if engine == "codex":
            thread = CodexCliThread(transcription, context=context, brief=brief)
        elif engine == "cloudflare":
            thread = GptQueryThread(None, transcription, engine="cloudflare",
                                    context=context, brief=brief, allow_web=True,
                                    fast=True)
        else:
            gpt_key = ApiKeyManager.load_api_key("openai")
            if not gpt_key:
                self.status_bar.showMessage(
                    "GPT necesita una API key de OpenAI (bot\u00f3n API key\u2026)")
                return
            thread = GptQueryThread(gpt_key, transcription,
                                    context=context, brief=brief, allow_web=True)

        header = "[Manual] " + transcription.replace("\n", " ")[:120]
        self._begin_gpt_stream(thread, header)
        self.overlay.gpt_update(thread, "…", header=header)
        if hasattr(thread, "query_delta"):
            thread.query_delta.connect(
                lambda txt, t=thread: self._update_gpt_stream(t, txt))
        self._gpt_threads.append(thread)
        thread.query_complete.connect(
            lambda ok, res, t=thread:
            self._handle_gpt_response(ok, res, t))
        thread.query_complete.connect(
            lambda *a, t=thread: self._gpt_threads.remove(t))
        self.send_to_gpt_button.setEnabled(False)
        self.status_bar.showMessage("Enviando a GPT\u2026")
        thread.start()

    def _handle_gpt_response(self, success, result, thread):
        self.send_to_gpt_button.setEnabled(True)
        if success:
            self._finish_gpt_stream(thread, result)
            self.overlay.gpt_update(thread, result)
            self.status_bar.showMessage("Respuesta GPT recibida")
        else:
            if thread in self._gpt_streams:
                self._finish_gpt_stream(thread, f"Error: {result}")
            self.overlay.gpt_update(thread, f"Error: {result}")
            self.status_bar.showMessage(f"Error de GPT: {result}")

    # ------------------------- cierre -------------------------

    def closeEvent(self, event):
        try:
            if getattr(self, "_ll_hook", None):
                ctypes.windll.user32.UnhookWindowsHookEx(self._ll_hook[0])
            self._retire_thread(self.capture_thread)
            for rt in self.realtimes.values():
                try:
                    rt.stop()
                except Exception:
                    pass
            self.realtimes = {}
            self.realtime = None
            self._retire_thread(self.transcription_worker)

            temp_dir = os.path.join(os.getcwd(), "temp_audio")
            if os.path.exists(temp_dir):
                removed = 0
                for name in os.listdir(temp_dir):
                    path = os.path.join(temp_dir, name)
                    try:
                        if os.path.isfile(path):
                            os.unlink(path)
                            removed += 1
                    except OSError:
                        pass
                try:
                    os.rmdir(temp_dir)
                except OSError:
                    pass
                print(f"Limpieza: {removed} temporales eliminados")
        except Exception as e:
            print(f"Error al cerrar: {e}")
        super().closeEvent(event)


def _force_light_palette(app):
    """Tema claro garantizado: ignora el modo oscuro del SO."""
    app.setStyle("Fusion")
    p = QPalette()
    text, base, win = QColor("#1a1a1a"), QColor("#ffffff"), QColor("#f4f4f4")
    p.setColor(QPalette.Window, win)
    p.setColor(QPalette.WindowText, text)
    p.setColor(QPalette.Base, base)
    p.setColor(QPalette.AlternateBase, QColor("#f0f0f0"))
    p.setColor(QPalette.Text, text)
    p.setColor(QPalette.Button, win)
    p.setColor(QPalette.ButtonText, text)
    p.setColor(QPalette.ToolTipBase, QColor("#ffffdc"))
    p.setColor(QPalette.ToolTipText, text)
    p.setColor(QPalette.Highlight, QColor("#2f6fed"))
    p.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    p.setColor(QPalette.PlaceholderText, QColor("#888888"))
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        p.setColor(QPalette.Disabled, role, QColor("#999999"))
    app.setPalette(p)


def main():
    app = QApplication(sys.argv)
    _force_light_palette(app)
    window = WhisperApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
