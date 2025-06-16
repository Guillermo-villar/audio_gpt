import os
from openai import OpenAI
from PyQt5.QtCore import QThread, pyqtSignal

class ApiKeyManager:
    """Gestiona el almacenamiento y recuperación de la API key de OpenAI"""
    
    @staticmethod
    def save_api_key(api_key):
        """Guarda la API key en un archivo local"""
        try:
            with open("api_key.txt", "w") as f:
                f.write(api_key)
            return True
        except Exception as e:
            print(f"Error al guardar API key: {e}")
            return False
    
    @staticmethod
    def load_api_key():
        """Carga la API key desde un archivo local"""
        try:
            if os.path.exists("api_key.txt"):
                with open("api_key.txt", "r") as f:
                    api_key = f.read().strip()
                    return api_key
            return ""
        except Exception as e:
            print(f"Error al cargar API key: {e}")
            return ""

class TranscriptionThread(QThread):
    """Hilo para transcribir audio con OpenAI Whisper"""
    transcription_complete = pyqtSignal(bool, str)
    
    def __init__(self, api_key, filename, language=None):
        super().__init__()
        self.api_key = api_key
        self.filename = filename
        self.language = language
    
    def run(self):
        try:
            # Inicializar cliente con nueva API
            client = OpenAI(api_key=self.api_key)
            
            # Abrir el archivo de audio
            with open(self.filename, "rb") as audio_file:
                # Preparar parámetros para la transcripción
                params = {
                    "model": "whisper-1",
                    "file": audio_file
                }
                if self.language and self.language != "":
                    params["language"] = self.language
                
                # Llamar a la API de OpenAI para transcribir con la nueva interfaz
                transcript = client.audio.transcriptions.create(**params)
                
                # Emitir el resultado
                self.transcription_complete.emit(True, transcript.text)
        except Exception as e:
            self.transcription_complete.emit(False, str(e))


class WhisperService:
    """Servicio para transcribir audio usando OpenAI Whisper"""
    
    @staticmethod
    def get_available_languages():
        """Devuelve un diccionario de idiomas disponibles para Whisper"""
        return {
            "": "Auto-detectar",
            "es": "Español",
            "en": "Inglés", 
            "fr": "Francés",
            "de": "Alemán",
            "it": "Italiano",
            "pt": "Portugués",
            "nl": "Holandés",
            "ru": "Ruso",
            "zh": "Chino",
            "ja": "Japonés",
            "ar": "Árabe"
        }
    
    @staticmethod
    def transcribe_file(api_key, file_path, language=None):
        """
        Transcribe un archivo de audio de forma sincrónica.
        Útil para scripts de línea de comandos.
        """
        try:
            # Inicializar cliente con nueva API
            client = OpenAI(api_key=api_key)
            
            with open(file_path, "rb") as audio_file:
                params = {
                    "model": "whisper-1",
                    "file": audio_file
                }
                if language and language != "":
                    params["language"] = language
                
                # Llamar a la API con la nueva interfaz
                transcript = client.audio.transcriptions.create(**params)
                return transcript.text
        except Exception as e:
            return f"[Error: {str(e)}]"
