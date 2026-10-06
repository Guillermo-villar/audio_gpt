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
import math
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
    QPlainTextEdit, QScrollArea, QTextBrowser, QToolButton, QSizePolicy,
)
from PySide6.QtCore import (
    Qt, QThread, Signal, Slot, QMutex, QTimer, QRect,
    QPropertyAnimation, QEasingCurve,
)
from PySide6.QtGui import (
    QPainter, QColor, QPen, QFont, QTextCursor, QPalette, QTextDocument,
    QTextFormat, QTextCharFormat, QFontMetrics,
)

import numpy as np
import sounddevice as sd
import soundfile as sf

import capture
import transcriber
import vad
from api_client import (
    ApiKeyManager, TranscriptionThread, WhisperService, GptClient,
    GptQueryThread, SmartQueryThread, CodexCliThread, looks_like_question,
    clef_question, CODEX_MODEL, DEFAULT_GPT_CONFIG, build_smart_request,
)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
TRANSCRIPTS_DIR = os.path.join(APP_DIR, "transcripts")

# Hotkeys globales de 2 teclas, vía WH_KEYBOARD_LL (eventos reales de
# tecla, sin admin ni deps). Elegidas porque NO hacen nada en
# Chrome/Edge/Firefox ni ES-keyboards; las teclas disparadas se consumen.
# Ctrl+Q responder · Alt+S más a fondo · Ctrl+M auto-GPT · Ctrl+I panel
# (único control de visibilidad) · Alt+G enviar a GPT · Alt+T start/stop.
# (Ojo: nunca Ctrl+Alt — AltGr en teclado ES = Ctrl+Alt y escribir «@»
# dispararía el hotkey.)
HOTKEYS = {
    "ctrl+q": (0x11, 0x51, "answer_last", "Ctrl+Q · responder última"),
    "alt+s": (0x12, 0x53, "smarter", "Alt+S · más a fondo"),
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
    "#8ab4f8", "#c58af9", "#fdd663", "#81c995", "#f28b82", "#a8c7fa",
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


def _apply_capture_exclusion(window, enabled):
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.SetWindowDisplayAffinity(
            int(window.winId()), 0x11 if enabled else 0x0)
    except Exception:
        pass


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
    _fired = set()

    def proc(ncode, wparam, lparam):
        try:
            if ncode == 0:
                if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    event = lparam.contents
                    if event.flags & 0x10:
                        return u32.CallNextHookEx(None, ncode, wparam, lparam)
                    vk = event.vkCode
                    swallow = False
                    for combo, (mod, key, _, _) in HOTKEYS.items():
                        if vk == key and _mod_state_ok(mod):
                            swallow = True
                            if combo not in _fired:
                                _fired.add(combo)
                                dispatch(combo)
                                if mod == 0x12:
                                    u32.keybd_event(0xE8, 0, 0, 0)
                                    u32.keybd_event(0xE8, 0, 0x0002, 0)
                    if swallow:
                        return 1
                elif wparam in (WM_KEYUP, WM_SYSKEYUP):
                    event = lparam.contents
                    if event.flags & 0x10:
                        return u32.CallNextHookEx(None, ncode, wparam, lparam)
                    vk = event.vkCode
                    swallow = False
                    for combo, (_, key, _, _) in HOTKEYS.items():
                        if vk == key:
                            if combo in _fired:
                                swallow = True
                                _fired.discard(combo)
                    if swallow:
                        return 1
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


def render_markdown(doc, text, font_px=13, color="#d4d6d9"):
    doc.setDefaultStyleSheet(
        f"body {{ color: {color}; font-size: {font_px}px; }}"
        "a { color: #8ab4f8; }"
        "table { border-collapse: collapse; }"
        "th, td { border: 1px solid #3a3d42; padding: 3px 6px; }"
    )
    features = (
        QTextDocument.MarkdownFeature.MarkdownDialectGitHub
        | QTextDocument.MarkdownFeature.MarkdownNoHTML
    )
    doc.setMarkdown(text or "", features)
    base_format = QTextCharFormat()
    base_format.setForeground(QColor(color))
    default_cursor = QTextCursor(doc)
    default_cursor.select(QTextCursor.SelectionType.Document)
    default_cursor.mergeCharFormat(base_format)
    code_fence = getattr(QTextFormat.Property, "BlockCodeFence", None)
    code_language = getattr(QTextFormat.Property, "BlockCodeLanguage", None)
    block = doc.begin()
    while block.isValid():
        block_format = block.blockFormat()
        block_char_format = block.charFormat()
        fenced = bool(block_char_format.fontFixedPitch())
        if code_fence is not None:
            fenced = fenced or bool(block_format.property(code_fence))
        if code_language is not None:
            fenced = fenced or bool(block_format.property(code_language))
        cursor = QTextCursor(block)
        cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
        if fenced:
            block_format.setBackground(QColor("#15171a"))
            block_format.setLeftMargin(8)
            block_format.setRightMargin(8)
            cursor.setBlockFormat(block_format)
            fmt = QTextCharFormat()
            fmt.setFontFamily("Consolas")
            fmt.setFontFixedPitch(True)
            fmt.setForeground(QColor("#d6d6d6"))
            fmt.setBackground(QColor("#15171a"))
            cursor.mergeCharFormat(fmt)
        else:
            fragments = []
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                if fragment.isValid():
                    fragments.append((
                        fragment.position(), fragment.length(),
                        fragment.charFormat()))
                iterator += 1
            for position, length, char_format in fragments:
                fmt = QTextCharFormat()
                if char_format.isAnchor():
                    fmt.setForeground(QColor("#8ab4f8"))
                if char_format.fontFixedPitch():
                    fmt.setBackground(QColor("#2a2d33"))
                    fmt.setForeground(QColor("#d6d6d6"))
                if char_format.font().strikeOut():
                    fmt.setForeground(QColor("#9aa0a6"))
                if fmt.hasProperty(QTextFormat.Property.ForegroundBrush) or \
                        fmt.hasProperty(QTextFormat.Property.BackgroundBrush):
                    fragment_cursor = QTextCursor(doc)
                    fragment_cursor.setPosition(position)
                    fragment_cursor.setPosition(
                        position + length,
                        QTextCursor.MoveMode.KeepAnchor)
                    fragment_cursor.mergeCharFormat(fmt)
        block = block.next()


class _AutoHeightBrowser(QTextBrowser):
    height_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._recomputing_height = False
        self.document().documentLayout().documentSizeChanged.connect(
            self.recompute_height)

    def recompute_height(self, *_):
        if self._recomputing_height:
            return
        self._recomputing_height = True
        try:
            margins = self.contentsMargins()
            height = (
                math.ceil(self.document().documentLayout().documentSize().height())
                + margins.top() + margins.bottom()
                + 2 * self.frameWidth() + 2
            )
            if self.height() != height:
                self.setFixedHeight(height)
                self.height_changed.emit()
        finally:
            self._recomputing_height = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.document().setTextWidth(self.viewport().width())
        self.recompute_height()


class AnswerVersion:
    def __init__(self, key, model_label, font_px, card):
        self.key = key
        self.card = card
        self.model_label = model_label
        self.text = ""
        self.done = False
        self.ok = True
        self.error_text = ""
        self.status_message = ""
        self.started_at = time.monotonic()
        self.finished_at = None
        self.expanded = False
        self._last_render = 0.0
        self._pending_render = False

        self.row = QWidget(card)
        self.row_layout = QVBoxLayout(self.row)
        self.row_layout.setContentsMargins(0, 0, 0, 0)
        self.row_layout.setSpacing(1)
        self.button = QToolButton(self.row)
        self.button.setAutoRaise(True)
        self.button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.button.setCursor(Qt.PointingHandCursor)
        self.button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.button.setStyleSheet(
            "QToolButton { border: none; background: transparent;"
            " color: #8e9297; font-size: 11px; text-align: left; padding: 0; }"
            "QToolButton:hover { color: #c4c7cc; }")
        self.button.clicked.connect(
            lambda _checked=False, version_key=key:
            card.toggle_version(version_key))
        self.row_layout.addWidget(self.button)

        self.body = _AutoHeightBrowser(self.row)
        body_font = QFont("Segoe UI")
        body_font.setPixelSize(font_px)
        self.body.setFont(body_font)
        self.body.setReadOnly(True)
        self.body.setOpenExternalLinks(False)
        self.body.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.body.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.body.setFrameShape(QFrame.NoFrame)
        self.body.setStyleSheet(
            "QTextBrowser { background: transparent; border: none; }")
        self.body.document().setDocumentMargin(0)
        self.body.height_changed.connect(card._body_height_changed)
        self.row_layout.addWidget(self.body)
        self.body.hide()
        self.row.hide()

        self._render_timer = QTimer(card)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._render_pending)

    def elapsed(self):
        end = self.finished_at or time.monotonic()
        return max(0.0, end - self.started_at)

    def elapsed_label(self):
        return f"{self.elapsed():.1f}s"

    def queue_render(self, final=False):
        if not final and self._last_render:
            remaining = 0.05 - (time.monotonic() - self._last_render)
            if remaining > 0:
                self._pending_render = True
                if not self._render_timer.isActive():
                    self._render_timer.start(
                        max(1, int(remaining * 1000) + 1))
                return
        self._render_timer.stop()
        self._pending_render = False
        self.card._render_version(self)

    def _render_pending(self):
        if self._pending_render:
            self._pending_render = False
            self.card._render_version(self)


class _ElidedLabel(QLabel):
    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self._full_text = text

    def resizeEvent(self, event):
        super().resizeEvent(event)
        metrics = QFontMetrics(self.font())
        self.setText(metrics.elidedText(
            self._full_text, Qt.ElideRight, max(1, event.size().width())))


class AnswerCard(QFrame):
    content_changed = Signal()

    def __init__(self, kind, model_label, question, font_px=13, parent=None,
                 key=None):
        super().__init__(parent)
        self.kind = kind
        self.font_px = font_px
        self.question = question or "(transcript completo)"
        self.versions = []
        self._versions_by_key = {}
        self._content_version = None
        self._main_body = None
        self.setObjectName("AnswerCard")
        self.setStyleSheet(
            "QFrame#AnswerCard { background: transparent; border: none; }"
            "QLabel { background: transparent; }")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(2)
        self.history_container = QWidget(self)
        self.history_layout = QVBoxLayout(self.history_container)
        self.history_layout.setContentsMargins(0, 0, 0, 0)
        self.history_layout.setSpacing(1)
        layout.addWidget(self.history_container)

        self.header_label = QLabel()
        self.header_label.setStyleSheet(
            "color: #8e9297; font-size: 11px; font-weight: normal;")
        layout.addWidget(self.header_label)

        self.question_label = _ElidedLabel(
            f"«…» {self.question.strip()}")
        self.question_label.setWordWrap(False)
        self.question_label.setStyleSheet(
            f"color: #8e9297; font-style: italic; "
            f"font-size: {11 if font_px <= 13 else 12}px;")
        layout.addWidget(self.question_label)

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.timeout.connect(self._refresh_status_lines)
        initial = self._new_version(key, model_label)
        self._content_version = initial
        self.body = initial.body
        self._refresh_version_layout()
        self._elapsed_timer.start(100)

    @property
    def current_version(self):
        return self._content_version

    @property
    def latest_version(self):
        return self.versions[-1]

    def _new_version(self, key, model_label):
        version = AnswerVersion(key, model_label, self.font_px, self)
        self.versions.append(version)
        self._versions_by_key[key] = version
        return version

    def add_version(self, key, model_label):
        for version in self.versions:
            version.expanded = False
        version = self._new_version(key, model_label)
        self._elapsed_timer.start(100)
        self._refresh_version_layout()
        self._refresh_status_lines()
        return version

    def _refresh_version_layout(self):
        while self.history_layout.count():
            item = self.history_layout.takeAt(0)
            if item.widget():
                item.widget().hide()

        current = self._content_version
        if self._main_body is not current.body:
            previous = next(
                (version for version in self.versions
                 if version.body is self._main_body),
                None)
            if previous is not None:
                self.layout().removeWidget(previous.body)
                previous.body.setParent(previous.row)
                previous.row_layout.addWidget(previous.body)
            current.row_layout.removeWidget(current.body)
            current.body.setParent(self)
            self.layout().addWidget(current.body)
            self._main_body = current.body
        current.body.show()
        self.body = current.body

        current_index = self.versions.index(current)
        for version in self.versions[:current_index]:
            if not version.text.strip():
                version.row.hide()
                continue
            if version.body.parent() is not version.row:
                self.layout().removeWidget(version.body)
                version.body.setParent(version.row)
                version.row_layout.addWidget(version.body)
            self._refresh_version_button(version)
            version.button.show()
            version.body.setVisible(version.expanded)
            self.history_layout.addWidget(version.row)
            version.row.show()

        self.history_container.setVisible(bool(self.history_layout.count()))
        self._refresh_status_lines()
        self.updateGeometry()
        self.content_changed.emit()

    def _refresh_version_button(self, version):
        arrow = "▾" if version.expanded else "▸"
        version.button.setText(
            f"{arrow} Anterior · {version.model_label} · "
            f"{version.elapsed_label()}")

    def _refresh_status_lines(self):
        latest = self.latest_version
        if latest.error_text:
            status = f"error: {latest.error_text}"
            self.header_label.setStyleSheet(
                "color: #e57373; font-size: 11px; font-weight: normal;")
        else:
            self.header_label.setStyleSheet(
                "color: #8e9297; font-size: 11px; font-weight: normal;")
            status = (
                latest.status_message
                or (latest.elapsed_label() if latest.done
                    else f"pensando… {latest.elapsed_label()}"))
        self.header_label.setText(f"{latest.model_label} · {status}")
        for version in self.versions:
            if version is not latest and version.text.strip():
                self._refresh_version_button(version)
        if not any(not version.done for version in self.versions):
            self._elapsed_timer.stop()

    def set_status(self, key, text):
        version = self._versions_by_key.get(key)
        if version is None:
            return
        version.status_message = text or ""
        self._refresh_status_lines()

    def set_stream_text(self, key, text, final=False, ok=True):
        version = self._versions_by_key.get(key)
        if version is None:
            return

        if not ok:
            version.ok = False
            version.done = True
            version.finished_at = time.monotonic()
            version.status_message = ""
            version.error_text = (text or "Error").removeprefix(
                "Error:").strip()
            version._render_timer.stop()
            version._pending_render = False
            if version.text.strip():
                self._render_version(version)
            self._refresh_status_lines()
            self.updateGeometry()
            self.content_changed.emit()
            return

        was_empty = not version.text.strip()
        version.text = text or ""
        version.status_message = ""
        if final:
            version.done = True
            version.finished_at = time.monotonic()
        version_index = self.versions.index(version)
        content_index = self.versions.index(self._content_version)
        if version.text.strip() and version_index >= content_index:
            previous = self._content_version
            if previous is not version:
                self._content_version = version
                self._refresh_version_layout()
                if previous.text.strip():
                    self._render_version(previous)
        elif version.text.strip() and was_empty:
            self._refresh_version_layout()

        version.queue_render(final=final)
        self._refresh_status_lines()

    def toggle_version(self, key):
        version = self._versions_by_key.get(key)
        if (version is None or version is self._content_version
                or not version.text.strip()):
            return
        version.expanded = not version.expanded
        self._refresh_version_button(version)
        version.body.setVisible(version.expanded)
        version.row.updateGeometry()
        self.history_container.updateGeometry()
        self.updateGeometry()
        self.content_changed.emit()

    def _render_version(self, version):
        color = (
            "#d4d6d9" if version is self._content_version else "#8e9297")
        render_markdown(
            version.body.document(), version.text, self.font_px, color=color)
        version._last_render = time.monotonic()
        version.body.recompute_height()
        version.body.updateGeometry()
        self.updateGeometry()
        self.content_changed.emit()

    def _body_height_changed(self):
        self.updateGeometry()
        self.content_changed.emit()

    def has_code_block(self):
        return any(
            re.search(r"```[^\n]*\n", version.text)
            for version in self._visible_code_versions())

    def longest_code_line(self):
        blocks = [
            block
            for version in self._visible_code_versions()
            for block in re.findall(
                r"```[^\n]*\n(.*?)(?:```|$)", version.text, re.S)
        ]
        return max(
            (len(line) for block in blocks for line in block.splitlines()),
            default=0)

    def _visible_code_versions(self):
        return [
            version for version in self.versions
            if version is self._content_version or version.expanded
        ]


class AnswerFeed(QScrollArea):
    content_changed = Signal()

    def __init__(self, font_px=13, parent=None):
        super().__init__(parent)
        self.font_px = font_px
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { background: #202226; width: 8px; }"
            "QScrollBar::handle:vertical { background: #4a4d53; border-radius: 4px; }"
        )
        self.container = QWidget()
        self.container.setStyleSheet("background: transparent;")
        self.layout = QVBoxLayout(self.container)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(2)
        self.layout.addStretch(1)
        self.setWidget(self.container)
        self._cards = []
        self._by_key = {}
        self._anchor = None
        self._setting_scroll = False
        self._content_change_pending = False
        self.verticalScrollBar().actionTriggered.connect(
            self._release_anchor)
        self.verticalScrollBar().sliderPressed.connect(
            self._release_anchor)

    def start(self, key, kind, model_label, question):
        self.clear()
        card = AnswerCard(
            kind, model_label, question, self.font_px, parent=self.container,
            key=key)
        card.content_changed.connect(self._schedule_content_changed)
        self._cards.append((key, card))
        self._by_key[key] = card
        self.layout.insertWidget(self.layout.count() - 1, card)
        self._anchor = card
        self._schedule_content_changed()
        return card

    def start_followup(self, parent_key, key, model_label):
        card = self._by_key.get(parent_key)
        if not isinstance(card, AnswerCard):
            return self.start(key, "Más a fondo", model_label, "")
        card.add_version(key, model_label)
        self._by_key[key] = card
        self._anchor = card
        self._schedule_content_changed()
        return card

    def update(self, key, text):
        card = self._by_key.get(key)
        if card:
            card.set_stream_text(key, text)
            self._schedule_content_changed()

    def status(self, key, text):
        card = self._by_key.get(key)
        if card:
            card.set_status(key, text)

    def finish(self, key, ok, text):
        card = self._by_key.get(key)
        if card:
            card.set_stream_text(key, text, final=True, ok=ok)
            self._schedule_content_changed()

    def clear(self):
        for _, card in self._cards:
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        self._by_key.clear()
        self._anchor = None
        self._schedule_content_changed()

    def content_height(self):
        return self.container.sizeHint().height()

    def _schedule_content_changed(self):
        if self._content_change_pending:
            return
        self._content_change_pending = True
        QTimer.singleShot(0, self._emit_content_changed)

    def _emit_content_changed(self):
        self._content_change_pending = False
        self.container.updateGeometry()
        self.content_changed.emit()
        QTimer.singleShot(0, self._emit_layout_content_changed)
        QTimer.singleShot(0, self._apply_anchor)

    def _emit_layout_content_changed(self):
        self.container.updateGeometry()
        self.content_changed.emit()

    def _release_anchor(self, *_):
        if not self._setting_scroll:
            self._anchor = None

    def _apply_anchor(self):
        if self._anchor is None:
            return
        top = self._anchor.mapTo(self.container, self._anchor.rect().topLeft()).y()
        bar = self.verticalScrollBar()
        target = max(0, min(top, bar.maximum()))
        self._setting_scroll = True
        bar.setValue(target)
        self._setting_scroll = False

    def wheelEvent(self, event):
        self._anchor = None
        super().wheelEvent(event)

    def has_code_block(self):
        return any(card.has_code_block() for _, card in self._cards)

    def longest_code_line(self):
        return max((card.longest_code_line()
                    for _, card in self._cards), default=0)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._apply_anchor)


class CompactOverlay(QWidget):
    """Panel compacto de respuestas que solo se muestra con Ctrl+I."""

    WIDTH = 460
    MIN_H = 200
    MIN_OPACITY = 0.30

    def __init__(self):
        super().__init__(
            None,
            Qt.Tool | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint
            | Qt.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.resize(self.WIDTH, 240)
        self._drag_pos = None
        self._listening = False
        self._opacity = 0.96
        self.setWindowOpacity(self._opacity)
        self._hide_from_capture = True
        self._live_lines = {"Entrevistador": [], "Tú": []}
        self._live_partial = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        panel = QFrame()
        panel.setObjectName("OverlayPanel")
        panel.setStyleSheet(
            "QFrame#OverlayPanel { background: rgba(24,26,29,238);"
            " border-radius: 10px; }"
            "QLabel { color: #d4d6d9; }"
        )
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(10, 8, 10, 8)
        panel_layout.setSpacing(6)

        self.status_label = QLabel("● en espera")
        self.status_label.setStyleSheet("color: #9aa0a6; font-size: 11px;")
        panel_layout.addWidget(self.status_label)

        self.interviewer_live = QLabel("")
        self.interviewer_live.setStyleSheet("color: #8ab4f8; font-size: 11px;")
        self.interviewer_live.setMaximumHeight(34)
        self.you_live = QLabel("")
        self.you_live.setStyleSheet("color: #81c995; font-size: 11px;")
        self.you_live.setMaximumHeight(34)
        panel_layout.addWidget(self.interviewer_live)
        panel_layout.addWidget(self.you_live)

        self.feed = AnswerFeed(font_px=13, parent=panel)
        panel_layout.addWidget(self.feed, 1)
        self.feed.content_changed.connect(self._schedule_resize)

        legend = QLabel(
            "Ctrl+Q responder · Alt+S más a fondo · Alt+G enviar · Ctrl+M auto "
            "· Alt+T start/stop · Ctrl+←↑→↓ mover · Ctrl+± opacidad "
            "· Ctrl+I ocultar")
        legend.setWordWrap(True)
        legend.setStyleSheet("color: #9aa0a6; font-size: 10px;")
        panel_layout.addWidget(legend)

        root.addWidget(panel)

        self._ack_timer = QTimer(self)
        self._ack_timer.setSingleShot(True)
        self._ack_timer.timeout.connect(self._restore_status)
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._resize_to_content)
        self._resize_animation = QPropertyAnimation(self, b"geometry", self)
        self._resize_animation.setDuration(240)
        self._resize_animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    # ------------------------- estado / ACKs -------------------------

    def _restore_status(self):
        color = "#81c995" if self._listening else "#9aa0a6"
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
        self.status_label.setStyleSheet("color: #fdd663; font-size: 11px;")
        self._ack_timer.start(1400)

    def set_live(self, lane, text, final=False):
        lane = lane if lane in self._live_lines else "Entrevistador"
        text = (text or "").strip()
        if final:
            if text:
                self._live_lines[lane].append(text)
                self._live_lines[lane] = self._live_lines[lane][-2:]
            self._live_partial.pop(lane, None)
        elif text:
            self._live_partial[lane] = text
        else:
            self._live_partial.pop(lane, None)
        self._render_live(lane)

    def _render_live(self, lane):
        label = (self.you_live if lane == "Tú" else self.interviewer_live)
        lines = self._live_lines[lane][-2:]
        partial = self._live_partial.get(lane)
        if partial:
            lines = (lines + [partial])[-2:]
        metrics = QFontMetrics(label.font())
        max_width = max(100, self.width() - 46)
        lane_label = "Tú: " if lane == "Tú" else "Entrevistador: "
        text = (lane_label + "\n".join(
            metrics.elidedText(line, Qt.TextElideMode.ElideRight, max_width)
            for line in lines) if lines else "")
        label.setText(text)

    # --------------------- tamaño / opacidad / posición ------------------

    def _schedule_resize(self, *_):
        self._resize_timer.start(120)

    def _resize_to_content(self):
        screen = self.screen() or QApplication.primaryScreen()
        available = screen.availableGeometry()
        max_height = int(available.height() * 0.85)
        if self.isVisible() and self.feed.height() > 0:
            chrome = self.height() - self.feed.height()
        else:
            chrome = (
                self.layout().sizeHint().height()
                - self.feed.sizeHint().height())
        target_height = int(max(
            self.MIN_H, min(chrome + self.feed.content_height(), max_height)))
        self.feed.setMaximumHeight(max(32, target_height - chrome))
        longest = self.feed.longest_code_line() if self.feed.has_code_block() else 0
        desired_width = 460
        if longest:
            desired_width = int(max(460, min(780, longest * 8.5 + 110)))
        desired_width = min(desired_width, available.width())
        x = min(max(self.x(), available.left()),
                available.right() - desired_width + 1)
        y = min(max(self.y(), available.top()),
                available.bottom() - target_height + 1)
        if (abs(target_height - self.height()) < 8
                and abs(desired_width - self.width()) < 8
                and (x, y) == (self.x(), self.y())):
            return
        self._resize_animation.stop()
        self._resize_animation.setStartValue(self.geometry())
        self._resize_animation.setEndValue(
            QRect(x, y, desired_width, target_height))
        self._resize_animation.start()

    def nudge(self, dx, dy):
        screen = self.screen().availableGeometry()
        x = min(max(self.x() + dx, screen.left()),
                screen.right() - self.width() + 1)
        y = min(max(self.y() + dy, screen.top()),
                screen.bottom() - self.height() + 1)
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

    def set_capture_exclusion(self, enabled):
        self._hide_from_capture = enabled
        _apply_capture_exclusion(self, enabled)

    def showEvent(self, event):
        super().showEvent(event)
        _apply_capture_exclusion(self, self._hide_from_capture)
        self._schedule_resize()


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
        self._hide_from_capture = True
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel()
        self.label.setStyleSheet(
            "background: rgba(24,26,29,238); color: #fdd663;"
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

    def set_capture_exclusion(self, enabled):
        self._hide_from_capture = enabled
        _apply_capture_exclusion(self, enabled)

    def showEvent(self, event):
        super().showEvent(event)
        _apply_capture_exclusion(self, self._hide_from_capture)


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
        painter.fillRect(self.rect(), QColor("#25272b"))
        width = int(self.width() * self.level)
        if self.level < 0.2:
            color = QColor("#2e7d4f")
        elif self.level < 0.6:
            color = QColor("#a88732")
        else:
            color = QColor("#a8423a")
        painter.fillRect(0, 0, width, self.height(), color)
        pen = QPen(QColor("#3a3d42"))
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
            self.loop_status.setStyleSheet("color: #81c995; font-weight: bold;")
        else:
            self.loop_status = QLabel(
                "No disponible. En Windows debería aparecer automáticamente; "
                "si no, usa VB-Cable o el micrófono."
            )
            self.loop_status.setStyleSheet("color: #e57373;")
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
    hotkey_requested = Signal(str)

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
        self._answers = []
        self._live_buffers = {}           # carril -> texto parcial acumulado
        self._gate_pending = set()        # carriles con gate clef en vuelo
        self._pinned = []                 # hechos fijados a mano, siempre en contexto
        self._hide_from_capture = self.settings.get("hide_from_capture", True)
        self._initial_splitter_sized = False

        self.init_ui()
        self.overlay = CompactOverlay()
        self.toast = AckToast()
        self.overlay.set_capture_exclusion(self._hide_from_capture)
        self.toast.set_capture_exclusion(self._hide_from_capture)
        self._apply_capture_setting()
        self.realtime_text.connect(self._append_transcript)
        self.realtime_error.connect(self.handle_continuous_error)
        self.gate_fired.connect(self._on_gate_fired)
        self._apply_settings()

        # Comandos por hook de teclado (eventos reales, sin falsos
        # positivos); el poll de 30 ms solo queda para los controles
        # mantenidos del panel (mover/opacidad).
        self.hotkey_requested.connect(
            self._dispatch_hotkey, Qt.ConnectionType.QueuedConnection)
        self._ll_hook = _install_ll_hook(self.hotkey_requested.emit)
        self._hk_timer = QTimer(self)
        self._hk_timer.timeout.connect(self._overlay_keys_tick)
        self._hk_timer.start(30)

    # ------------------------- construcción de UI -------------------------

    def init_ui(self):
        self.setWindowTitle("audio_gpt — Transcriptor y asistente")
        self.setGeometry(100, 100, 980, 760)
        self.setStyleSheet(
            "QTextEdit, QPlainTextEdit { background: #25272b; color: #d4d6d9;"
            " border: 1px solid #3a3d42; border-radius: 6px; }"
            "QLineEdit, QComboBox, QSpinBox { background: #25272b;"
            " color: #d4d6d9; border: 1px solid #3a3d42;"
            " border-radius: 6px; padding: 3px; }"
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

        self.hide_capture_checkbox = QCheckBox(
            "Invisible al compartir pantalla / grabar")
        self.hide_capture_checkbox.toggled.connect(
            self._set_capture_exclusion)
        tr_layout.addWidget(self.hide_capture_checkbox)

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
            edit.setMaximumHeight(220)
            col.addWidget(edit)
            live = QLabel("En directo: \u2014")
            live.setWordWrap(True)
            live.setStyleSheet("color: #9aa0a6; font-style: italic;")
            col.addWidget(live)
            return col, edit, live

        col_int, self.interviewer_output, self.interviewer_live = _lane_pane(
            "Entrevistador", "#8ab4f8", "Voces de la llamada…")
        col_you, self.you_output, self.you_live = _lane_pane(
            "Tú (micro)", "#81c995", "Tu voz…")
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
        hk_hint = QLabel(
            "Ctrl+I panel · Alt+G / Ctrl+Q enviar · Alt+S más a fondo")
        hk_hint.setStyleSheet("color: #888; font-size: 11px;")
        self.send_to_gpt_button = QPushButton("Enviar a GPT")
        self.send_to_gpt_button.setStyleSheet(
            "QPushButton { background-color: #5b4a8a; color: #d4d6d9;"
            " border-radius: 8px; padding: 6px 18px; }"
            "QPushButton:hover { background-color: #6d5b9e; }"
            "QPushButton:pressed { background-color: #4d3f78; }"
        )
        self.send_to_gpt_button.setMinimumSize(170, 44)
        self.send_to_gpt_button.setFont(QFont("Segoe UI", 12, QFont.Bold))
        self.send_to_gpt_button.clicked.connect(self.send_to_gpt)
        self.smarter_button = QPushButton("Más a fondo")
        self.smarter_button.setMinimumSize(140, 44)
        self.smarter_button.setFont(QFont("Segoe UI", 11, QFont.Bold))
        self.smarter_button.setStyleSheet(
            "QPushButton { background: #2b2d31; color: #cdb8ff;"
            " border: 1px solid #b08cff; border-radius: 8px;"
            " padding: 6px 14px; }"
            "QPushButton:hover { background: #34303f; }"
        )
        self.smarter_button.clicked.connect(self._hk_smarter)
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
        self.gpt_engine_combo.setMinimumHeight(44)
        text_buttons.addWidget(self.gpt_engine_combo)
        text_buttons.addWidget(self.send_to_gpt_button)
        text_buttons.addWidget(self.smarter_button)
        out_layout.addLayout(text_buttons)
        bottom_layout.addWidget(out_group, 2)

        gpt_group = QGroupBox("Respuestas GPT")
        gpt_layout = QVBoxLayout(gpt_group)
        self.gpt_output = AnswerFeed(font_px=15)
        self.gpt_output.setMinimumHeight(260)
        gpt_layout.addWidget(self.gpt_output)
        gpt_group.setMinimumHeight(300)
        bottom_layout.addWidget(gpt_group, 3)

        splitter.addWidget(bottom)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Listo")

        self._on_provider_changed()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._initial_splitter_sized:
            self._initial_splitter_sized = True
            QTimer.singleShot(0, self._set_initial_splitter_sizes)

    def _set_initial_splitter_sizes(self):
        height = self.main_splitter.height()
        if height <= 0 or not self.config_panel.isVisible():
            return
        bottom_height = round(height * 0.55)
        self.main_splitter.setSizes([height - bottom_height, bottom_height])
        self._expanded_splitter_sizes = self.main_splitter.sizes()

    def toggle_configuration(self, hidden):
        if hidden:
            self._expanded_splitter_sizes = self.main_splitter.sizes()
        self.config_panel.setVisible(not hidden)
        self.config_toggle_button.setText(
            "Mostrar configuración" if hidden else "Ocultar configuración")
        if not hidden:
            self.main_splitter.setSizes(self._expanded_splitter_sizes)

    def _style_continuous_button(self, start):
        color, hover, pressed = (
            ("#2e7d4f", "#388e5b", "#276b43") if start
            else ("#a8423a", "#bb5047", "#963a33")
        )
        self.continuous_button.setStyleSheet(
            f"QPushButton {{ background-color: {color}; color: #d4d6d9; border-radius: 8px; }}"
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
        self.hide_capture_checkbox.setChecked(
            s.get("hide_from_capture", True))
        self._hide_from_capture = self.hide_capture_checkbox.isChecked()
        self._apply_capture_setting()

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
            "hide_from_capture": self.hide_capture_checkbox.isChecked(),
        })

    def _apply_capture_setting(self):
        _apply_capture_exclusion(self, self._hide_from_capture)
        if hasattr(self, "overlay"):
            self.overlay.set_capture_exclusion(self._hide_from_capture)
        if hasattr(self, "toast"):
            self.toast.set_capture_exclusion(self._hide_from_capture)

    def _set_capture_exclusion(self, enabled):
        self._hide_from_capture = bool(enabled)
        self._apply_capture_setting()
        if hasattr(self, "hide_capture_checkbox"):
            self._persist_settings()

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

    def _hk_smarter(self):
        target = self._answers[-1] if self._answers else None
        if target is None:
            self._ack("Alt+S · nada que mejorar todavía")
            return
        self._ack("Alt+S · más a fondo con gpt-6.1-sol…")
        self._launch_smarter(target)

    def _launch_smarter(self, target):
        config = GptClient.load_config() or dict(DEFAULT_GPT_CONFIG)
        question = target["question"]
        previous = []
        current = target
        while current is not None:
            previous.append((
                current["model"], current["text"],
                current.get("parent") is not None))
            current = (self._answer_for_key(current["parent"])
                       if current.get("parent") is not None else None)
        previous.reverse()
        ctx = list(self._ctx)
        mine = "\n".join(
            line for line in ctx[target["ctx_index"]:]
            if line.startswith("Tú:"))
        context = self._build_context()
        engine = self.gpt_engine_combo.currentData()
        brief = self.brief_input.toPlainText().strip()
        effort = ("high" if "sol" in target["model"].lower()
                  else config.get("smart_reasoning_effort", "medium"))
        instructions, user_input = build_smart_request(
            question, previous, context, mine, brief, config)

        if engine == "codex":
            model = config.get("smart_model", "gpt-6.1-sol")
            thread = CodexCliThread(
                question, model=model,
                full_prompt=instructions + "\n\n" + user_input, search=True)
        elif engine == "cloudflare":
            openai_key = ApiKeyManager.load_api_key("openai")
            if openai_key:
                model = config.get("smart_model", "gpt-6.1-sol")
                thread = SmartQueryThread(
                    openai_key, question, previous, context, mine, brief,
                    effort=effort)
            else:
                model = config.get("cf_model", DEFAULT_GPT_CONFIG["cf_model"])
                thread = SmartQueryThread(
                    None, question, previous, context, mine, brief,
                    effort=effort, engine="cloudflare")
        else:
            openai_key = ApiKeyManager.load_api_key("openai")
            if not openai_key:
                self.status_bar.showMessage(
                    "Más a fondo necesita una API key de OpenAI")
                return
            model = config.get("smart_model", "gpt-6.1-sol")
            thread = SmartQueryThread(
                openai_key, question, previous, context, mine, brief,
                effort=effort)

        model_label = model + (
            " · web" if config.get("smart_web_search", True) else "")
        self._start_answer(
            thread, "Más a fondo", model_label, question, context,
            parent=target["key"])
        self._gpt_threads.append(thread)
        thread.start()

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
            self.overlay.set_live(lane, text, final=True)
            self._draft_state.pop(lane, None)   # turno cerrado: próximo borrador
            self._on_new_segment(text, lane)
            return

        if self._current_provider() == "openai-realtime":
            text = self._live_buffers.get(lane, "") + text
        self._live_buffers[lane] = text
        self._set_live_transcript(lane, text)
        self.overlay.set_live(lane, text)
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
        if hasattr(self, "overlay"):
            self.overlay.set_live("Entrevistador", "")
            self.overlay.set_live("Tú", "")
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

        question = text
        if lane and question.startswith(lane + ":"):
            question = question.split(":", 1)[1].strip()
        model_label = self._model_label(engine, kind)
        self._start_answer(thread, kind, model_label, question, context)
        self._gpt_threads.append(thread)
        thread.start()

    def _model_label(self, engine, kind):
        config = GptClient.load_config() or dict(DEFAULT_GPT_CONFIG)
        if engine == "codex":
            return CODEX_MODEL
        if engine == "cloudflare":
            key = "cf_fast_model" if kind == "Borrador" else "cf_model"
            return config.get(key, DEFAULT_GPT_CONFIG[key])
        return config.get("model", DEFAULT_GPT_CONFIG["model"])

    def _start_answer(self, thread, kind, model_label, question, context,
                      parent=None, manual=False):
        answer = {
            "key": thread,
            "kind": kind,
            "model": model_label,
            "question": question,
            "ctx_index": len(self._ctx),
            "text": "",
            "done": False,
            "ok": True,
            "parent": parent,
            "manual": manual,
        }
        self._answers.append(answer)
        if parent is None:
            self.gpt_output.start(thread, kind, model_label, question)
            self.overlay.feed.start(thread, kind, model_label, question)
        else:
            self.gpt_output.start_followup(parent, thread, model_label)
            self.overlay.feed.start_followup(parent, thread, model_label)
        if hasattr(thread, "query_delta"):
            thread.query_delta.connect(
                lambda text, key=thread: self._update_answer(key, text))
        if hasattr(thread, "query_status"):
            thread.query_status.connect(
                lambda text, key=thread: self._status_answer(key, text))
        thread.query_complete.connect(
            lambda ok, result, key=thread:
            self._finish_answer(key, ok, result))
        thread.query_complete.connect(
            lambda *args, key=thread:
            self._gpt_threads.remove(key) if key in self._gpt_threads else None)
        return answer

    def _answer_for_key(self, key):
        return next((answer for answer in reversed(self._answers)
                     if answer["key"] is key), None)

    def _update_answer(self, key, text):
        answer = self._answer_for_key(key)
        if answer is None:
            return
        answer["text"] = text
        self.gpt_output.update(key, text)
        self.overlay.feed.update(key, text)

    def _status_answer(self, key, text):
        self.gpt_output.status(key, text)
        self.overlay.feed.status(key, text)

    def _finish_answer(self, key, success, result):
        answer = self._answer_for_key(key)
        if answer is None:
            return
        answer["done"] = True
        answer["ok"] = success
        answer["text"] = result if success else f"Error: {result}"
        self.gpt_output.finish(key, success, answer["text"])
        self.overlay.feed.finish(key, success, answer["text"])
        if answer.get("manual"):
            self.send_to_gpt_button.setEnabled(True)
        self.status_bar.showMessage(
            "Respuesta GPT recibida" if success else f"GPT: {result}")

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
        self.overlay.feed.clear()
        self._ctx.clear()            # estado oculto no sobrevive al «Limpiar»
        self._draft_state.clear()
        self._pinned.clear()
        self._answers.clear()
        self.status_bar.showMessage("Transcripción y contexto borrados")

    def _latest_interviewer_question(self):
        for line in reversed(self._ctx):
            if not line.startswith("Tú:"):
                return line.split(":", 1)[1].strip() if ":" in line else line
        return "(transcript completo)"

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

        self._start_answer(
            thread, "Manual", self._model_label(engine, "Borrador"),
            self._latest_interviewer_question(), context, manual=True)
        self._gpt_threads.append(thread)
        self.send_to_gpt_button.setEnabled(False)
        self.status_bar.showMessage("Enviando a GPT\u2026")
        thread.start()

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


def _apply_dark_palette(app):
    app.setStyle("Fusion")
    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: "#1e1f22",
        QPalette.ColorRole.Base: "#25272b",
        QPalette.ColorRole.AlternateBase: "#2b2d31",
        QPalette.ColorRole.Text: "#d4d6d9",
        QPalette.ColorRole.WindowText: "#d4d6d9",
        QPalette.ColorRole.Button: "#2b2d31",
        QPalette.ColorRole.ButtonText: "#d4d6d9",
        QPalette.ColorRole.ToolTipBase: "#2b2d31",
        QPalette.ColorRole.ToolTipText: "#d4d6d9",
        QPalette.ColorRole.Highlight: "#3d6bb3",
        QPalette.ColorRole.HighlightedText: "#f0f0f0",
        QPalette.ColorRole.PlaceholderText: "#7c8087",
        QPalette.ColorRole.Link: "#8ab4f8",
        QPalette.ColorRole.Light: "#3b3d42",
        QPalette.ColorRole.Midlight: "#313338",
        QPalette.ColorRole.Dark: "#17181b",
        QPalette.ColorRole.Mid: "#222428",
        QPalette.ColorRole.Shadow: "#101114",
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText,
                 QPalette.ColorRole.WindowText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor("#6b6f75"))
    app.setPalette(p)
    app.setStyleSheet("""
        QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox,
        QDoubleSpinBox, QDateEdit, QTimeEdit {
            background-color: #25272b;
            color: #d4d6d9;
            border: 1px solid #3a3d42;
            border-radius: 6px;
            padding: 5px;
            selection-background-color: #3d6bb3;
        }
        QPushButton {
            background-color: #2b2d31;
            color: #d4d6d9;
            border: 1px solid #3a3d42;
            border-radius: 6px;
            padding: 6px 10px;
        }
        QPushButton:hover { background-color: #37393e; }
        QToolTip {
            color: #d4d6d9;
            background-color: #2b2d31;
            border: 1px solid #3a3d42;
        }
    """)


def main():
    app = QApplication(sys.argv)
    _apply_dark_palette(app)
    window = WhisperApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
