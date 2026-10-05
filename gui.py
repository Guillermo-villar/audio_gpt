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

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QPushButton, QVBoxLayout, QHBoxLayout,
    QWidget, QLabel, QSpinBox, QTextEdit, QLineEdit, QComboBox,
    QProgressBar, QFileDialog, QMessageBox, QGroupBox, QStatusBar,
    QDialog, QDialogButtonBox, QFrame, QSplitter, QCheckBox,
)
from PySide6.QtCore import Qt, QThread, Signal, Slot, QMutex
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QTextCursor

import numpy as np
import sounddevice as sd
import soundfile as sf

import capture
import transcriber
import vad
from api_client import (
    ApiKeyManager, TranscriptionThread, WhisperService, GptQueryThread,
    CodexCliThread,
)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")


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
            self.loop_status = QLabel(f"OK — capturando de: {loopback.name}")
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
        self.copy_button = QPushButton("Copiar respuesta")
        self.copy_button.clicked.connect(self.copy_response)
        self.save_button = QPushButton("Guardar respuesta")
        self.save_button.clicked.connect(self.save_response)
        self.close_button = QPushButton("Cerrar")
        self.close_button.clicked.connect(self.accept)
        button_layout.addWidget(self.copy_button)
        button_layout.addWidget(self.save_button)
        button_layout.addStretch()
        button_layout.addWidget(self.close_button)
        layout.addLayout(button_layout)

    def copy_response(self):
        text = self.response_text.toPlainText()
        if text:
            QApplication.clipboard().setText(text)
            QMessageBox.information(self, "Información", "Respuesta copiada al portapapeles")

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

        self.init_ui()
        self.realtime_text.connect(self._append_transcript)
        self.realtime_error.connect(self.handle_continuous_error)
        self._apply_settings()

    # ------------------------- construcción de UI -------------------------

    def init_ui(self):
        self.setWindowTitle("audio_gpt — Transcriptor y asistente")
        self.setGeometry(100, 100, 980, 760)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        self.continuous_button = QPushButton("INICIAR TRANSCRIPCIÓN CONTINUA")
        self.continuous_button.setMinimumHeight(56)
        self.continuous_button.setFont(QFont("Arial", 12, QFont.Bold))
        self._style_continuous_button(start=True)
        self.continuous_button.clicked.connect(self.toggle_continuous_mode)
        main_layout.addWidget(self.continuous_button)

        splitter = QSplitter(Qt.Vertical)
        main_layout.addWidget(splitter, 1)

        top = QWidget()
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
        manual_layout.addStretch()
        tr_layout.addLayout(manual_layout)

        top_layout.addWidget(tr_group)
        splitter.addWidget(top)

        # --- salidas ---
        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)

        out_group = QGroupBox("Transcripción")
        out_layout = QVBoxLayout(out_group)
        self.transcription_output = QTextEdit()
        self.transcription_output.setReadOnly(True)
        self.transcription_output.setPlaceholderText("La transcripción aparecerá aquí…")
        out_layout.addWidget(self.transcription_output)

        text_buttons = QHBoxLayout()
        self.copy_button = QPushButton("Copiar")
        self.save_button = QPushButton("Guardar")
        self.clear_button = QPushButton("Limpiar")
        self.copy_button.clicked.connect(self.copy_text)
        self.save_button.clicked.connect(self.save_text)
        self.clear_button.clicked.connect(self.clear_text)
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
        text_buttons.addStretch()
        self.gpt_engine_combo = QComboBox()
        self.gpt_engine_combo.addItem("API OpenAI", "openai")
        self.gpt_engine_combo.addItem("Codex CLI (ChatGPT sub)", "codex")
        self.gpt_engine_combo.setToolTip(
            "API OpenAI: pago por uso (necesita API key). "
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

    def _persist_settings(self):
        _save_settings({
            "provider": self.provider_selector.currentData(),
            "model": self.model_selector.currentData(),
            "language": self.language_selector.currentData(),
            "source": self.source_selector.currentData(),
            "gpt_engine": self.gpt_engine_combo.currentData(),
        })

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
        if needs_key and not self.api_key:
            dialog = ApiKeyDialog(self, provider)
            if dialog.exec() == QDialog.Accepted:
                self.api_key = dialog.get_api_key()

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
            self.transcription_output.setPlainText(result)
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
        self.transcription_output.clear()
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
        self.status_bar.showMessage("Transcripción continua iniciada")

    def _start_realtime(self, language, model):
        """Crea el/los transcriptor(es) streaming según proveedor y fuente."""
        provider = self._current_provider()
        source = self._current_source()

        def make_rt(lane=""):
            cb = (lambda t, fin, l=lane: self.realtime_text.emit(t, fin, l))
            err = (lambda m: self.realtime_error.emit(m))
            if provider == "deepgram":
                return transcriber.DeepgramRealtime(
                    self.api_key, model=model, language=language,
                    on_transcript=cb, on_error=err)
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

    @Slot(str, bool, str)
    def _append_transcript(self, text, is_final, lane=""):
        if is_final:
            self._on_new_segment(text, lane)

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

    def _on_new_segment(self, text, lane=""):
        """Llamado con cada fragmento nuevo transcrito (modo continuo)."""
        display = f"{lane}: {text}" if lane else text
        current = self.transcription_output.toPlainText()
        self.transcription_output.setPlainText(
            f"{current}\n{display}" if current else display)
        cursor = self.transcription_output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.transcription_output.setTextCursor(cursor)

        if self.auto_gpt_checkbox.isChecked() and text.strip():
            gpt_input = display  # la etiqueta de carril da contexto a GPT
            if self.gpt_engine_combo.currentData() == "codex":
                thread = CodexCliThread(gpt_input)
            else:
                gpt_key = ApiKeyManager.load_api_key("openai")
                if not gpt_key:
                    self.status_bar.showMessage(
                        "Auto-GPT necesita una API key de OpenAI (botón «API key…»)"
                    )
                    return
                thread = GptQueryThread(gpt_key, gpt_input)
            self._gpt_threads.append(thread)
            thread.query_complete.connect(self._on_auto_gpt_result)
            thread.query_complete.connect(
                lambda *a, t=thread: self._gpt_threads.remove(t))
            thread.start()

    def _on_auto_gpt_result(self, success, result):
        if success:
            existing = self.gpt_output.toPlainText()
            self.gpt_output.setPlainText(f"{existing}\n\n{result}" if existing else result)
            cursor = self.gpt_output.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.gpt_output.setTextCursor(cursor)
        else:
            self.status_bar.showMessage(f"GPT: {result}")

    def handle_continuous_error(self, error_msg):
        if not self.is_continuous_mode:
            return  # coalesce: errores en vuelo tras la primera parada
        self.stop_continuous_mode()       # detener antes de abrir el modal
        QMessageBox.warning(self, "Error", error_msg)

    # ------------------------- texto / gpt manual -------------------------

    def copy_text(self):
        text = self.transcription_output.toPlainText()
        if text:
            QApplication.clipboard().setText(text)
            self.status_bar.showMessage("Copiado al portapapeles")

    def save_text(self):
        text = self.transcription_output.toPlainText()
        if not text:
            QMessageBox.warning(self, "Advertencia", "No hay texto para guardar")
            return
        filename, _ = QFileDialog.getSaveFileName(
            self, "Guardar transcripción", "",
            "Archivos de texto (*.txt);;Todos los archivos (*)")
        if filename:
            try:
                with open(filename, "w", encoding="utf-8") as f:
                    f.write(text)
                self.status_bar.showMessage(f"Guardado en {filename}")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Error al guardar: {e}")

    def clear_text(self):
        self.transcription_output.clear()
        self.gpt_output.clear()
        self.status_bar.showMessage("Transcripción borrada")

    def send_to_gpt(self):
        transcription = self.transcription_output.toPlainText()
        if not transcription:
            QMessageBox.warning(self, "Advertencia", "No hay texto para enviar a GPT")
            return

        if self.gpt_engine_combo.currentData() == "codex":
            thread = CodexCliThread(transcription)
        else:
            gpt_key = ApiKeyManager.load_api_key("openai")
            if not gpt_key:
                QMessageBox.warning(
                    self, "Error",
                    "GPT usa la API de OpenAI: configura una API key con «API key…» "
                    "seleccionando el proveedor OpenAI."
                )
                return
            thread = GptQueryThread(gpt_key, transcription)

        wait_dialog = QMessageBox(self)
        wait_dialog.setWindowTitle("Procesando")
        wait_dialog.setText("Enviando a GPT…")
        wait_dialog.setStandardButtons(QMessageBox.NoButton)
        wait_dialog.setIcon(QMessageBox.Information)

        self.gpt_thread = thread
        self.gpt_thread.query_complete.connect(
            lambda ok, res: self._handle_gpt_response(ok, res, wait_dialog, transcription))
        wait_dialog.show()
        self.send_to_gpt_button.setEnabled(False)   # evita reemplazar el hilo en curso
        self.gpt_thread.start()

    def _handle_gpt_response(self, success, result, wait_dialog, transcription):
        wait_dialog.accept()
        self.send_to_gpt_button.setEnabled(True)
        if success:
            existing = self.gpt_output.toPlainText()
            self.gpt_output.setPlainText(
                f"{existing}\n\n{result}" if existing else result)
            GptResponseDialog(self, transcription, result).exec()
        else:
            QMessageBox.critical(self, "Error", f"Error de GPT: {result}")

    # ------------------------- cierre -------------------------

    def closeEvent(self, event):
        try:
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


def main():
    app = QApplication(sys.argv)
    window = WhisperApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
