import sys
import os
import time
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QPushButton, QVBoxLayout, QHBoxLayout, 
    QWidget, QLabel, QSpinBox, QTextEdit, QLineEdit, QComboBox,
    QProgressBar, QFileDialog, QMessageBox, QGroupBox, QStatusBar,
    QDialog, QDialogButtonBox, QFrame, QSplitter
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QSize
from PyQt5.QtGui import QPainter, QColor, QPen, QIcon, QFont

# Importar módulos propios
import recorder
from api_client import ApiKeyManager, TranscriptionThread, WhisperService

import sounddevice as sd
import numpy as np
import soundfile as sf
import uuid

class AudioLevelMonitor(QThread):
    """Hilo para monitorear niveles de audio en tiempo real"""
    level_updated = pyqtSignal(float)
    
    def __init__(self, device_index):
        super().__init__()
        self.device_index = device_index
        self.running = False
        self.samplerate = 44100
    
    def run(self):
        self.running = True
        
        def callback(indata, frames, time, status):
            if self.running:
                volume_norm = np.linalg.norm(indata) / np.sqrt(frames)
                self.level_updated.emit(volume_norm)
        
        try:
            with sd.InputStream(device=self.device_index, channels=2, callback=callback,
                              blocksize=int(self.samplerate * 0.1),
                              samplerate=self.samplerate):
                while self.running:
                    sd.sleep(100)
        except Exception as e:
            print(f"Error en monitoreo de audio: {e}")
    
    def stop(self):
        self.running = False

class AudioLevelWidget(QFrame):
    """Widget para visualizar niveles de audio en tiempo real"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(30)
        self.setFrameShape(QFrame.StyledPanel)
        self.level = 0.0
        
    def set_level(self, level):
        self.level = min(level * 5, 1.0)  # Amplificar para mejor visualización
        self.update()
        
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        
        # Dibujar fondo
        painter.fillRect(self.rect(), QColor(30, 30, 30))
        
        # Dibujar barra de nivel
        width = int(self.width() * self.level)
        if self.level < 0.2:
            color = QColor(0, 180, 0)  # Verde para niveles bajos
        elif self.level < 0.6:
            color = QColor(180, 180, 0)  # Amarillo para niveles medios
        else:
            color = QColor(180, 0, 0)  # Rojo para niveles altos
            
        painter.fillRect(0, 0, width, self.height(), color)
        
        # Dibujar marcas de nivel
        pen = QPen(QColor(100, 100, 100))
        painter.setPen(pen)
        for i in range(1, 10):
            x = int(self.width() * i / 10)
            painter.drawLine(x, 0, x, self.height())

class AudioDeviceSetupDialog(QDialog):
    """Diálogo para configurar los dispositivos de audio"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configuración de Dispositivos de Audio")
        self.setMinimumWidth(600)
        self.setModal(True)
        
        layout = QVBoxLayout(self)
        
        # Dispositivos de entrada
        input_group = QGroupBox("Dispositivos de Entrada (Grabación)")
        input_layout = QVBoxLayout(input_group)
        
        self.input_devices = QComboBox()
        self.refresh_input_button = QPushButton("Actualizar Lista")
        
        input_device_layout = QHBoxLayout()
        input_device_layout.addWidget(QLabel("Dispositivo:"))
        input_device_layout.addWidget(self.input_devices)
        input_device_layout.addWidget(self.refresh_input_button)
        
        input_layout.addLayout(input_device_layout)
        
        # Dispositivos de salida
        output_group = QGroupBox("Dispositivos de Salida (Reproducción)")
        output_layout = QVBoxLayout(output_group)
        
        self.output_devices = QComboBox()
        self.refresh_output_button = QPushButton("Actualizar Lista")
        
        output_device_layout = QHBoxLayout()
        output_device_layout.addWidget(QLabel("Dispositivo:"))
        output_device_layout.addWidget(self.output_devices)
        output_device_layout.addWidget(self.refresh_output_button)
        
        output_layout.addLayout(output_device_layout)
        
        # Virtual Cable
        vb_group = QGroupBox("Configuración de Virtual Cable")
        vb_layout = QVBoxLayout(vb_group)
        
        self.check_vb_button = QPushButton("Verificar Configuración de Virtual Cable")
        self.vb_status = QLabel("Estado: No verificado")
        
        vb_layout.addWidget(self.check_vb_button)
        vb_layout.addWidget(self.vb_status)
        
        # Monitor de audio
        monitor_group = QGroupBox("Monitor de Niveles de Audio")
        monitor_layout = QVBoxLayout(monitor_group)
        
        self.level_widget = AudioLevelWidget()
        self.monitor_button = QPushButton("Iniciar Monitoreo")
        self.monitor_device = QComboBox()
        
        monitor_device_layout = QHBoxLayout()
        monitor_device_layout.addWidget(QLabel("Dispositivo a monitorear:"))
        monitor_device_layout.addWidget(self.monitor_device)
        
        monitor_layout.addLayout(monitor_device_layout)
        monitor_layout.addWidget(self.level_widget)
        monitor_layout.addWidget(self.monitor_button)
        
        # Agregar grupos al layout principal
        layout.addWidget(input_group)
        layout.addWidget(output_group)
        layout.addWidget(vb_group)
        layout.addWidget(monitor_group)
        
        # Botones
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        
        # Conectar señales
        self.refresh_input_button.clicked.connect(self.load_input_devices)
        self.refresh_output_button.clicked.connect(self.load_output_devices)
        self.check_vb_button.clicked.connect(self.check_virtual_cable)
        self.monitor_button.clicked.connect(self.toggle_monitor)
        
        # Cargar dispositivos inicialmente
        self.load_input_devices()
        self.load_output_devices()
        self.load_monitor_devices()
        
        # Inicializar monitor
        self.monitor_thread = None
        self.monitoring = False
    
    def load_input_devices(self):
        """Cargar dispositivos de entrada de audio"""
        self.input_devices.clear()
        devices = sd.query_devices()
        for i, device in enumerate(devices):
            if device['max_input_channels'] > 0:
                self.input_devices.addItem(f"{device['name']}", i)
    
    def load_output_devices(self):
        """Cargar dispositivos de salida de audio"""
        self.output_devices.clear()
        devices = sd.query_devices()
        for i, device in enumerate(devices):
            if device['max_output_channels'] > 0:
                self.output_devices.addItem(f"{device['name']}", i)
    
    def load_monitor_devices(self):
        """Cargar dispositivos para monitorear"""
        self.monitor_device.clear()
        devices = sd.query_devices()
        for i, device in enumerate(devices):
            if device['max_input_channels'] > 0:
                self.monitor_device.addItem(f"{device['name']}", i)
    
    def check_virtual_cable(self):
        """Verifica la configuración de Virtual Cable"""
        self.setCursor(Qt.WaitCursor)
        try:
            # Comprobar si VB-Cable está instalado
            cable_input_idx = recorder.find_device_by_name('cable input')
            cable_output_idx = recorder.find_device_by_name('cable output')
            
            if cable_input_idx is None or cable_output_idx is None:
                self.vb_status.setText("❌ Virtual Cable no encontrado o no instalado correctamente")
                self.vb_status.setStyleSheet("color: red;")
                
                if QMessageBox.question(
                    self, "Virtual Cable no encontrado", 
                    "VB-Cable no está instalado o no se detecta.\n¿Deseas visitar el sitio de descarga?",
                    QMessageBox.Yes | QMessageBox.No
                ) == QMessageBox.Yes:
                    import webbrowser
                    webbrowser.open("https://vb-audio.com/Cable/")
            else:
                self.vb_status.setText("✅ Virtual Cable detectado correctamente")
                self.vb_status.setStyleSheet("color: green;")
                
                # Mostrar configuración adicional
                QMessageBox.information(
                    self, "Configuración de Virtual Cable", 
                    "Para usar Virtual Cable correctamente:\n\n"
                    "1. Configura 'CABLE Input' como dispositivo de salida predeterminado en Windows\n"
                    "2. Reproduce algún audio en tu sistema mientras grabas\n\n"
                    "¿Deseas abrir la configuración de audio de Windows?",
                    QMessageBox.Yes | QMessageBox.No
                )
                
                if QMessageBox.Yes:
                    try:
                        import subprocess
                        subprocess.run("start ms-settings:sound", shell=True)
                    except:
                        QMessageBox.warning(
                            self, "Error", 
                            "No se pudo abrir la configuración automáticamente.\n"
                            "Por favor, abre manualmente: Panel de control > Sonido > Reproducción"
                        )
        except Exception as e:
            self.vb_status.setText(f"❌ Error al verificar: {e}")
            self.vb_status.setStyleSheet("color: red;")
        finally:
            self.setCursor(Qt.ArrowCursor)
    
    def toggle_monitor(self):
        """Inicia o detiene el monitoreo de audio"""
        if not self.monitoring:
            # Iniciar monitoreo
            try:
                device_index = self.monitor_device.currentData()
                if device_index is not None:
                    self.monitor_thread = AudioLevelMonitor(device_index)
                    self.monitor_thread.level_updated.connect(self.level_widget.set_level)
                    self.monitor_thread.start()
                    self.monitoring = True
                    self.monitor_button.setText("Detener Monitoreo")
            except Exception as e:
                QMessageBox.warning(self, "Error", f"No se pudo iniciar el monitoreo: {e}")
        else:
            # Detener monitoreo
            if self.monitor_thread:
                self.monitor_thread.stop()
                self.monitor_thread = None
            self.monitoring = False
            self.monitor_button.setText("Iniciar Monitoreo")
    
    def closeEvent(self, event):
        """Limpiar recursos al cerrar"""
        if self.monitor_thread:
            self.monitor_thread.stop()
            self.monitor_thread = None
        super().closeEvent(event)
    
    def get_selected_devices(self):
        """Retorna los dispositivos seleccionados"""
        return {
            'input': self.input_devices.currentData(),
            'output': self.output_devices.currentData()
        }

class AudioRecordTranscribeThread(QThread):
    update_progress = pyqtSignal(int)
    update_partial_transcript = pyqtSignal(str)
    recording_complete = pyqtSignal(bool, str)

    def __init__(self, filename, duration, device_index, api_key, language_code):
        super().__init__()
        self.filename = filename
        self.duration = duration
        self.device_index = device_index
        self.api_key = api_key
        self.language_code = language_code
        self.samplerate = 48000
        self.channels = 2
        self.chunk_duration = 5  # segundos por fragmento para transcribir

    def run(self):
        try:
            total_frames = int(self.duration * self.samplerate)
            chunk_frames = int(self.chunk_duration * self.samplerate)
            audio_buffer = np.zeros((0, self.channels), dtype=np.float32)
            recorded = []
            start_time = time.time()
            stream = sd.InputStream(device=self.device_index, channels=self.channels, samplerate=self.samplerate)
            stream.start()
            frames_recorded = 0
            partial_transcript = ""
            while frames_recorded < total_frames:
                frames_to_read = min(chunk_frames, total_frames - frames_recorded)
                chunk = stream.read(frames_to_read)[0]
                audio_buffer = np.concatenate((audio_buffer, chunk))
                frames_recorded += frames_to_read
                # Guardar fragmento temporal para transcripción
                temp_chunk_file = self.filename + ".chunk.wav"
                import soundfile as sf
                sf.write(temp_chunk_file, audio_buffer, self.samplerate)
                # Llamar a Whisper para transcribir el fragmento
                try:
                    from api_client import WhisperService
                    partial = WhisperService.transcribe_file(self.api_key, temp_chunk_file, self.language_code)
                    if partial:
                        partial_transcript = partial
                        self.update_partial_transcript.emit(partial_transcript)
                except Exception as e:
                    self.update_partial_transcript.emit(f"[Error transcribiendo: {e}]")
                self.update_progress.emit(int(100 * frames_recorded / total_frames))
            stream.stop()
            # Guardar audio completo
            sf.write(self.filename, audio_buffer, self.samplerate)
            self.recording_complete.emit(True, self.filename)
        except Exception as e:
            self.recording_complete.emit(False, str(e))

class ApiKeyDialog(QDialog):
    """Diálogo para solicitar la API key de OpenAI al iniciar la aplicación"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configuración de API Key")
        self.setModal(True)
        self.setFixedSize(500, 200)
        
        layout = QVBoxLayout(self)
        
        # Título y descripción
        title_label = QLabel("Configuración inicial requerida")
        title_label.setStyleSheet("font-size: 16px; font-weight: bold; margin-bottom: 10px;")
        layout.addWidget(title_label)
        
        description_label = QLabel(
            "Para usar la transcripción de audio, necesitas una API key de OpenAI.\n"
            "Puedes obtenerla en: https://platform.openai.com/api-keys\n\n"
            "La API key se guardará localmente para futuros usos."
        )
        description_label.setWordWrap(True)
        layout.addWidget(description_label)
        
        # Campo para la API key
        api_key_layout = QHBoxLayout()
        api_key_label = QLabel("API Key:")
        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.Password)
        self.api_key_input.setPlaceholderText("sk-...")
        api_key_layout.addWidget(api_key_label)
        api_key_layout.addWidget(self.api_key_input)
        layout.addLayout(api_key_layout)
        
        # Botones
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        
        # Validar que la API key no esté vacía
        buttons.button(QDialogButtonBox.Ok).clicked.connect(self.validate_and_save)
        
        # Foco en el campo de texto
        self.api_key_input.setFocus()
    
    def validate_and_save(self):
        """Valida y guarda la API key"""
        api_key = self.api_key_input.text().strip()
        
        if not api_key:
            QMessageBox.warning(self, "Error", "Por favor, ingresa una API key válida.")
            return
        
        if not api_key.startswith("sk-"):
            reply = QMessageBox.question(
                self, "Confirmación", 
                "La API key no parece tener el formato correcto (debería empezar con 'sk-').\n"
                "¿Estás seguro de que quieres continuar?",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.No:
                return
        
        # Guardar la API key
        if ApiKeyManager.save_api_key(api_key):
            QMessageBox.information(self, "Éxito", "API key guardada correctamente.")
            self.accept()
        else:
            QMessageBox.critical(self, "Error", "Error al guardar la API key.")
    
    def get_api_key(self):
        """Retorna la API key ingresada"""
        return self.api_key_input.text().strip()

class AudioRecorderThread(QThread):
    """Hilo para grabar audio sin bloquear la interfaz"""
    update_progress = pyqtSignal(int)
    update_level = pyqtSignal(float)
    recording_complete = pyqtSignal(bool, str)
    
    def __init__(self, filename, duration, use_virtual_cable, device_index=None):
        super().__init__()
        self.filename = filename
        self.duration = duration
        self.use_virtual_cable = use_virtual_cable
        self.device_index = device_index
        self.samplerate = 48000
        self.channels = 2
    
    def run(self):
        try:
            if self.use_virtual_cable:
                # Usar cable virtual para grabar audio del sistema
                cable_output_idx = recorder.find_device_by_name('cable output')
                if cable_output_idx is not None:
                    # Configurar para grabar desde CABLE Output
                    device_idx = cable_output_idx
                else:
                    self.recording_complete.emit(False, "No se encontró el dispositivo Virtual Cable")
                    return
            else:
                # Usar el dispositivo seleccionado
                device_idx = self.device_index
            
            # Iniciar grabación
            total_frames = int(self.duration * self.samplerate)
            audio_buffer = np.zeros((0, self.channels), dtype=np.float32)
            
            # Configurar stream
            stream = sd.InputStream(device=device_idx, channels=self.channels, samplerate=self.samplerate)
            stream.start()
            
            frames_recorded = 0
            start_time = time.time()
            
            # Grabar en chunks para actualizar progreso
            while frames_recorded < total_frames:
                # Determinar tamaño de chunk
                chunk_size = min(int(self.samplerate * 0.1), total_frames - frames_recorded)
                
                # Leer chunk
                chunk, overflowed = stream.read(chunk_size)
                audio_buffer = np.concatenate((audio_buffer, chunk))
                
                # Calcular nivel de audio y emitir señal
                level = np.linalg.norm(chunk) / np.sqrt(chunk_size)
                self.update_level.emit(level)
                
                # Actualizar contador y progreso
                frames_recorded += chunk_size
                progress = int(100 * frames_recorded / total_frames)
                self.update_progress.emit(progress)
                
                # No sobrecargar la CPU
                time.sleep(0.01)
            
            # Detener stream
            stream.stop()
            stream.close()
            
            # Guardar archivo
            import soundfile as sf
            sf.write(self.filename, audio_buffer, self.samplerate)
            
            # Verificar si el audio contiene sonido real
            if recorder.verificar_audio(self.filename):
                self.recording_complete.emit(True, self.filename)
            else:
                self.recording_complete.emit(False, "La grabación contiene solo silencio. Verifica la configuración.")
                
        except Exception as e:
            self.recording_complete.emit(False, str(e))

class ContinuousRecordTranscribeThread(QThread):
    """Hilo para grabar y transcribir audio continuamente"""
    update_level = pyqtSignal(float)
    update_transcription = pyqtSignal(str)
    status_update = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    
    def __init__(self, api_key, device_index, language_code, chunk_duration=10):
        super().__init__()
        self.api_key = api_key
        self.device_index = device_index
        self.language_code = language_code
        self.running = False
        self.samplerate = 48000
        self.channels = 2
        self.chunk_duration = chunk_duration  # segundos por fragmento
        self.temp_dir = os.path.join(os.getcwd(), "temp_audio")
        
        # Crear directorio temporal si no existe
        if not os.path.exists(self.temp_dir):
            os.makedirs(self.temp_dir)
            
        # Transcripción acumulada
        self.full_transcription = ""
    
    def run(self):
        try:
            self.running = True
            self.status_update.emit("Iniciando grabación continua...")
            
            # Configurar stream
            stream = sd.InputStream(
                device=self.device_index, 
                channels=self.channels, 
                samplerate=self.samplerate
            )
            stream.start()
            
            chunk_frames = int(self.chunk_duration * self.samplerate)
            
            while self.running:
                # 1. Grabar fragmento
                self.status_update.emit("Grabando fragmento de audio...")
                audio_buffer = np.zeros((0, self.channels), dtype=np.float32)
                
                for _ in range(0, chunk_frames, 4800):  # Leer en bloques pequeños
                    if not self.running:
                        break
                        
                    frames_to_read = min(4800, chunk_frames - len(audio_buffer))
                    if frames_to_read <= 0:
                        break
                        
                    chunk, overflowed = stream.read(frames_to_read)
                    audio_buffer = np.concatenate((audio_buffer, chunk))
                    
                    # Actualizar nivel de audio
                    level = np.linalg.norm(chunk) / np.sqrt(len(chunk))
                    self.update_level.emit(level)
                    
                    # Pequeña pausa para no sobrecargar la CPU
                    time.sleep(0.01)
                
                if not self.running:
                    break
                
                # 2. Guardar fragmento en archivo temporal
                temp_file = os.path.join(self.temp_dir, f"chunk_{uuid.uuid4()}.wav")
                sf.write(temp_file, audio_buffer, self.samplerate)
                
                # Verificar si hay audio real
                if not recorder.verificar_audio(temp_file):
                    self.status_update.emit("El fragmento contiene solo silencio, continuando...")
                    continue
                
                # 3. Transcribir fragmento
                self.status_update.emit("Transcribiendo fragmento...")
                try:
                    from api_client import WhisperService
                    transcription = WhisperService.transcribe_file(
                        self.api_key, temp_file, self.language_code
                    )
                    
                    if transcription and transcription != "[Error" and len(transcription) > 0:
                        # Añadir a la transcripción completa
                        if self.full_transcription:
                            self.full_transcription += " " + transcription
                        else:
                            self.full_transcription = transcription
                            
                        # Emitir la transcripción completa
                        self.update_transcription.emit(self.full_transcription)
                        self.status_update.emit(f"Transcripción actualizada ({len(transcription)} caracteres)")
                    else:
                        self.status_update.emit("No se detectó texto en el fragmento")
                        
                except Exception as e:
                    self.error_occurred.emit(f"Error al transcribir: {str(e)}")
                
                # Eliminar archivo temporal
                try:
                    os.remove(temp_file)
                except:
                    pass
            
            # Cerrar stream cuando se detiene
            stream.stop()
            stream.close()
            self.status_update.emit("Grabación continua detenida")
            
        except Exception as e:
            self.running = False
            self.error_occurred.emit(f"Error en grabación continua: {str(e)}")
    
    def stop(self):
        """Detiene la grabación continua"""
        self.running = False

class WhisperApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.api_key = None
        self.current_audio_file = None
        self.recorder_thread = None
        self.transcription_thread = None
        self.selected_input_device = None
        self.selected_output_device = None
        self.audio_level_monitor = None
        self.continuous_thread = None
        self.is_continuous_mode = False
        
        # Verificar y obtener API key antes de inicializar la UI
        if not self.setup_api_key():
            # Si el usuario cancela el diálogo, cerrar la aplicación
            sys.exit()
        
        self.init_ui()
    
    def setup_api_key(self):
        """Verifica si existe API key guardada o solicita una nueva"""
        # Intentar cargar API key existente
        saved_api_key = ApiKeyManager.load_api_key()
        
        if saved_api_key:
            self.api_key = saved_api_key
            return True
        
        # Si no existe, mostrar diálogo para pedirla
        dialog = ApiKeyDialog(self)
        if dialog.exec_() == QDialog.Accepted:
            self.api_key = dialog.get_api_key()
            return True
        
        return False
    
    def init_ui(self):
        # Configuración de la ventana principal
        self.setWindowTitle("Grabadora y Transcriptor de Audio")
        self.setGeometry(100, 100, 900, 700)
        
        # Widget central y layout principal
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        
        # Botón grande de inicio/detención de transcripción continua
        self.continuous_button_layout = QHBoxLayout()
        self.continuous_button = QPushButton("INICIAR TRANSCRIPCIÓN CONTINUA")
        self.continuous_button.setMinimumHeight(60)
        self.continuous_button.setFont(QFont("Arial", 12, QFont.Bold))
        self.continuous_button.setStyleSheet(
            "QPushButton { background-color: #4CAF50; color: white; border-radius: 8px; }"
            "QPushButton:hover { background-color: #45a049; }"
            "QPushButton:pressed { background-color: #398438; }"
        )
        self.continuous_button.clicked.connect(self.toggle_continuous_mode)
        self.continuous_button_layout.addWidget(self.continuous_button)
        
        main_layout.addLayout(self.continuous_button_layout)
        
        # Splitter para dividir la interfaz
        splitter = QSplitter(Qt.Vertical)
        main_layout.addWidget(splitter, 1)
        
        # --- Sección de configuración de grabación ---
        top_widget = QWidget()
        top_layout = QVBoxLayout(top_widget)
        
        recording_group = QGroupBox("Configuración de grabación")
        recording_layout = QVBoxLayout(recording_group)
        
        # Selector de modo de grabación
        mode_layout = QHBoxLayout()
        mode_label = QLabel("Método de grabación:")
        self.mode_selector = QComboBox()
        self.mode_selector.addItem("Virtual Cable (audio del sistema)")
        self.mode_selector.addItem("Micrófono u otro dispositivo")
        mode_layout.addWidget(mode_label)
        mode_layout.addWidget(self.mode_selector)
        
        # Botón de configuración de audio
        self.audio_setup_button = QPushButton("Configurar Dispositivos")
        mode_layout.addWidget(self.audio_setup_button)
        
        recording_layout.addLayout(mode_layout)
        
        # Duración de grabación
        duration_layout = QHBoxLayout()
        duration_label = QLabel("Duración (segundos):")
        self.duration_input = QSpinBox()
        self.duration_input.setRange(1, 300)
        self.duration_input.setValue(30)
        duration_layout.addWidget(duration_label)
        duration_layout.addWidget(self.duration_input)
        recording_layout.addLayout(duration_layout)
        
        # Monitor de nivel de audio
        self.level_monitor = AudioLevelWidget()
        recording_layout.addWidget(self.level_monitor)
        
        # Botones de grabación
        button_layout = QHBoxLayout()
        self.record_button = QPushButton("Grabar")
        self.play_button = QPushButton("Reproducir")
        self.play_button.setEnabled(False)
        button_layout.addWidget(self.record_button)
        button_layout.addWidget(self.play_button)
        recording_layout.addLayout(button_layout)
        
        # Barra de progreso
        self.progress_bar = QProgressBar()
        recording_layout.addWidget(self.progress_bar)
        
        top_layout.addWidget(recording_group)
        
        # --- Sección de OpenAI Whisper ---
        whisper_group = QGroupBox("Transcripción con OpenAI Whisper")
        whisper_layout = QVBoxLayout(whisper_group)
        
        # Estado de la API Key
        api_status_layout = QHBoxLayout()
        api_status_label = QLabel("Estado de API Key:")
        self.api_status_text = QLabel("✅ Configurada correctamente")
        self.api_status_text.setStyleSheet("color: green; font-weight: bold;")
        self.change_api_button = QPushButton("Cambiar API Key")
        self.change_api_button.clicked.connect(self.change_api_key)
        api_status_layout.addWidget(api_status_label)
        api_status_layout.addWidget(self.api_status_text)
        api_status_layout.addStretch()
        api_status_layout.addWidget(self.change_api_button)
        whisper_layout.addLayout(api_status_layout)
        
        # Selector de idioma
        language_layout = QHBoxLayout()
        language_label = QLabel("Idioma:")
        self.language_selector = QComboBox()
        
        # Cargar idiomas disponibles
        languages = WhisperService.get_available_languages()
        for code, name in languages.items():
            self.language_selector.addItem(name, code)
            
        language_layout.addWidget(language_label)
        language_layout.addWidget(self.language_selector)
        whisper_layout.addLayout(language_layout)
        
        # Botón de transcripción
        self.transcribe_button = QPushButton("Transcribir")
        self.transcribe_button.setEnabled(False)
        whisper_layout.addWidget(self.transcribe_button)
        
        top_layout.addWidget(whisper_group)
        splitter.addWidget(top_widget)
        
        # --- Sección de resultado ---
        bottom_widget = QWidget()
        bottom_layout = QVBoxLayout(bottom_widget)
        
        result_group = QGroupBox("Transcripción")
        result_layout = QVBoxLayout(result_group)
        
        self.transcription_output = QTextEdit()
        self.transcription_output.setReadOnly(True)
        result_layout.addWidget(self.transcription_output)
        
        # Botones para texto
        text_button_layout = QHBoxLayout()
        self.copy_button = QPushButton("Copiar")
        self.save_button = QPushButton("Guardar")
        self.clear_button = QPushButton("Limpiar")
        text_button_layout.addWidget(self.copy_button)
        text_button_layout.addWidget(self.save_button)
        text_button_layout.addWidget(self.clear_button)
        result_layout.addLayout(text_button_layout)
        
        bottom_layout.addWidget(result_group)
        splitter.addWidget(bottom_widget)
        
        # Barra de estado
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Listo")
        
        # Conectar señales y slots
        self.connect_signals()
    
    def connect_signals(self):
        # Botones de grabación
        self.record_button.clicked.connect(self.start_recording)
        self.play_button.clicked.connect(self.play_audio)
        self.audio_setup_button.clicked.connect(self.show_audio_setup)
        
        # Botón de transcripción
        self.transcribe_button.clicked.connect(self.transcribe_audio)
        
        # Botones de texto
        self.copy_button.clicked.connect(self.copy_text)
        self.save_button.clicked.connect(self.save_text)
        self.clear_button.clicked.connect(self.clear_text)
    
    def toggle_continuous_mode(self):
        """Alterna entre iniciar y detener la transcripción continua"""
        if not self.is_continuous_mode:
            # Iniciar modo continuo
            self.start_continuous_mode()
        else:
            # Detener modo continuo
            self.stop_continuous_mode()
    
    def start_continuous_mode(self):
        """Inicia la grabación y transcripción continua"""
        # Verificar API key
        if not self.api_key:
            QMessageBox.warning(self, "Error", "No hay API key configurada")
            return
        
        # Obtener dispositivo de audio según el modo seleccionado
        if self.mode_selector.currentIndex() == 0:  # Virtual Cable
            device_idx = recorder.find_device_by_name('cable output')
            if device_idx is None:
                QMessageBox.warning(
                    self, "Error", 
                    "No se encontró el dispositivo Virtual Cable. Verifica la configuración."
                )
                return
        else:
            # Usar dispositivo seleccionado o el predeterminado
            device_idx = self.selected_input_device
            if device_idx is None:
                # Intentar usar el dispositivo de entrada predeterminado
                try:
                    device_idx = sd.query_devices(kind='input')['index']
                except:
                    QMessageBox.warning(
                        self, "Error", 
                        "No se ha seleccionado un dispositivo de entrada. Usa 'Configurar Dispositivos'.",
                        QMessageBox.Ok
                    )
                    return
        
        # Obtener idioma seleccionado
        selected_language = self.language_selector.currentData()
        
        # Desactivar elementos de UI durante la transcripción continua
        self.record_button.setEnabled(False)
        self.play_button.setEnabled(False)
        self.transcribe_button.setEnabled(False)
        self.audio_setup_button.setEnabled(False)
        self.mode_selector.setEnabled(False)
        self.language_selector.setEnabled(False)
        self.duration_input.setEnabled(False)
        
        # Cambiar estilo del botón a rojo (detener)
        self.continuous_button.setText("DETENER TRANSCRIPCIÓN CONTINUA")
        self.continuous_button.setStyleSheet(
            "QPushButton { background-color: #f44336; color: white; border-radius: 8px; }"
            "QPushButton:hover { background-color: #e53935; }"
            "QPushButton:pressed { background-color: #c62828; }"
        )
        
        # Limpiar transcripción anterior
        self.transcription_output.clear()
        
        # Crear y configurar el hilo de transcripción continua
        self.continuous_thread = ContinuousRecordTranscribeThread(
            self.api_key, device_idx, selected_language
        )
        
        # Conectar señales
        self.continuous_thread.update_level.connect(self.update_audio_level)
        self.continuous_thread.update_transcription.connect(self.update_continuous_transcription)
        self.continuous_thread.status_update.connect(self.status_bar.showMessage)
        self.continuous_thread.error_occurred.connect(self.handle_continuous_error)
        
        # Iniciar hilo
        self.continuous_thread.start()
        self.is_continuous_mode = True
        
        self.status_bar.showMessage("Transcripción continua iniciada")
    
    def stop_continuous_mode(self):
        """Detiene la grabación y transcripción continua"""
        if self.continuous_thread:
            self.continuous_thread.stop()
            self.continuous_thread = None
        
        # Restaurar interfaz
        self.record_button.setEnabled(True)
        self.audio_setup_button.setEnabled(True)
        self.mode_selector.setEnabled(True)
        self.language_selector.setEnabled(True)
        self.duration_input.setEnabled(True)
        
        # Restaurar botón a verde (iniciar)
        self.continuous_button.setText("INICIAR TRANSCRIPCIÓN CONTINUA")
        self.continuous_button.setStyleSheet(
            "QPushButton { background-color: #4CAF50; color: white; border-radius: 8px; }"
            "QPushButton:hover { background-color: #45a049; }"
            "QPushButton:pressed { background-color: #398438; }"
        )
        
        self.is_continuous_mode = False
        self.status_bar.showMessage("Transcripción continua detenida")
    
    def update_continuous_transcription(self, text):
        """Actualiza el campo de texto con la transcripción continua"""
        self.transcription_output.setPlainText(text)
        # Desplazar automáticamente hacia abajo
        self.transcription_output.moveCursor(self.transcription_output.textCursor().End)
    
    def handle_continuous_error(self, error_msg):
        """Maneja errores en la transcripción continua"""
        QMessageBox.warning(self, "Error", error_msg)
        self.stop_continuous_mode()
    
    def show_audio_setup(self):
        """Muestra el diálogo de configuración de audio"""
        dialog = AudioDeviceSetupDialog(self)
        if dialog.exec_() == QDialog.Accepted:
            selected_devices = dialog.get_selected_devices()
            self.selected_input_device = selected_devices['input']
            self.selected_output_device = selected_devices['output']
            self.status_bar.showMessage("Configuración de dispositivos actualizada")
    
    def update_audio_level(self, level):
        """Actualiza el widget de nivel de audio"""
        self.level_monitor.set_level(level)
    
    def start_recording(self):
        # Desactivar botones durante la grabación
        self.record_button.setEnabled(False)
        self.transcribe_button.setEnabled(False)
        self.play_button.setEnabled(False)
        
        # Crear nombre de archivo temporal
        self.current_audio_file = os.path.join(os.getcwd(), "recording.wav")
        
        # Obtener duración y método
        duration = self.duration_input.value()
        use_virtual_cable = self.mode_selector.currentIndex() == 0
        
        # Iniciar hilo de grabación
        self.recorder_thread = AudioRecorderThread(
            self.current_audio_file, 
            duration, 
            use_virtual_cable,
            self.selected_input_device
        )
        self.recorder_thread.update_progress.connect(self.update_progress)
        self.recorder_thread.update_level.connect(self.update_audio_level)
        self.recorder_thread.recording_complete.connect(self.recording_finished)
        
        # Reiniciar barra de progreso
        self.progress_bar.setValue(0)
        
        # Iniciar grabación
        self.status_bar.showMessage("Grabando audio...")
        self.recorder_thread.start()
    
    def update_progress(self, value):
        self.progress_bar.setValue(value)
    
    def recording_finished(self, success, message):
        # Reactivar botones
        self.record_button.setEnabled(True)
        
        if success:
            self.status_bar.showMessage("Grabación completada")
            self.play_button.setEnabled(True)
            self.transcribe_button.setEnabled(True)
        else:
            self.status_bar.showMessage(f"Error: {message}")
            QMessageBox.critical(self, "Error", f"Error durante la grabación: {message}")
            # Sugerir solución si es por silencio
            if "silencio" in message.lower():
                if self.mode_selector.currentIndex() == 0:  # Virtual Cable
                    QMessageBox.information(
                        self, "Sugerencia", 
                        "La grabación está silenciosa. Verifica que:\n\n"
                        "1. CABLE Input está configurado como dispositivo de salida predeterminado\n"
                        "2. Estás reproduciendo algún sonido mientras grabas\n"
                        "3. El volumen del sistema no está silenciado\n\n"
                        "Puedes usar el botón 'Configurar Dispositivos' para verificar la configuración."
                    )
    
    def play_audio(self):
        if self.current_audio_file and os.path.exists(self.current_audio_file):
            recorder.reproducir_audio(self.current_audio_file)
        else:
            QMessageBox.warning(self, "Error", "No hay archivo de audio para reproducir")
    
    def change_api_key(self):
        """Permite cambiar la API key actual"""
        dialog = ApiKeyDialog(self)
        if dialog.exec_() == QDialog.Accepted:
            self.api_key = dialog.get_api_key()
            self.status_bar.showMessage("API key actualizada correctamente")
    
    def transcribe_audio(self):
        # Verificar que tenemos audio grabado
        if not self.current_audio_file or not os.path.exists(self.current_audio_file):
            QMessageBox.warning(self, "Error", "No hay archivo de audio para transcribir")
            return
        
        # Verificar API key (ahora usa la almacenada)
        if not self.api_key:
            QMessageBox.warning(self, "Error", "No hay API key configurada")
            return
        
        # Obtener idioma seleccionado
        selected_language = self.language_selector.currentData()
        
        # Desactivar botón durante la transcripción
        self.transcribe_button.setEnabled(False)
        self.status_bar.showMessage("Transcribiendo audio...")
        
        # Iniciar hilo de transcripción
        self.transcription_thread = TranscriptionThread(
            self.api_key, self.current_audio_file, selected_language
        )
        self.transcription_thread.transcription_complete.connect(self.transcription_finished)
        self.transcription_thread.start()
    
    def transcription_finished(self, success, result):
        # Reactivar botón
        self.transcribe_button.setEnabled(True)
        
        if success:
            self.transcription_output.setPlainText(result)
            self.status_bar.showMessage("Transcripción completada")
        else:
            QMessageBox.critical(self, "Error", f"Error durante la transcripción: {result}")
            self.status_bar.showMessage(f"Error: {result}")
    
    def copy_text(self):
        text = self.transcription_output.toPlainText()
        if text:
            clipboard = QApplication.clipboard()
            clipboard.setText(text)
            self.status_bar.showMessage("Texto copiado al portapapeles")
    
    def save_text(self):
        text = self.transcription_output.toPlainText()
        if not text:
            QMessageBox.warning(self, "Advertencia", "No hay texto para guardar")
            return
        
        filename, _ = QFileDialog.getSaveFileName(
            self, "Guardar transcripción", "", "Archivos de texto (*.txt);;Todos los archivos (*)"
        )
        
        if filename:
            try:
                with open(filename, "w", encoding="utf-8") as f:
                    f.write(text)
                self.status_bar.showMessage(f"Texto guardado en {filename}")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Error al guardar el archivo: {e}")
    
    def clear_text(self):
        self.transcription_output.clear()
        self.status_bar.showMessage("Transcripción borrada")
    
    def save_api_key(self):
        api_key = self.api_key_input.text().strip()
        if api_key:
            ApiKeyManager.save_api_key(api_key)

def main():
    app = QApplication(sys.argv)
    window = WhisperApp()
    window.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
