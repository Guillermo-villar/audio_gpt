import os
import json
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

class GptClient:
    """Cliente para comunicarse con la API de GPT"""
    
    @staticmethod
    def load_config():
        """Carga la configuración del modelo GPT desde el archivo JSON"""
        config_path = os.path.join(os.path.dirname(__file__), "gpt_config.json")
        
        if not os.path.exists(config_path):
            # Si no existe el archivo, crear uno con configuración predeterminada
            default_config = {
                "model": "gpt-3.5-turbo",
                "system_prompt": "Eres un asistente virtual experto que ayuda a los usuarios a comprender y procesar información. Tu tarea es analizar el texto proporcionado (que viene de una transcripción de audio) y ofrecer respuestas claras, útiles y bien estructuradas. Trata de buscar preguntas en la transcripción, y da de la manera más concisa posible la respuesta a esas preguntas. El texto puede incluir carácteres de otros idiomas por fallo de la transcripción, pero ignora texto que no esté en inglés o español.",
                "temperature": 0.6,
                "max_tokens": 1000,
                "top_p": 1,
                "frequency_penalty": 0,
                "presence_penalty": 0
            }
            
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(default_config, f, indent=2)
            
            return default_config
        
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error al cargar la configuración GPT: {e}")
            return None
    
    @staticmethod
    def send_to_gpt(api_key, transcription):
        """Envía la transcripción a GPT y devuelve la respuesta"""
        try:
            config = GptClient.load_config()
            if not config:
                return False, "Error al cargar la configuración de GPT"
            
            client = OpenAI(api_key=api_key)
            
            # Formatear la transcripción para que comience con "Transcription: "
            formatted_transcription = f"Transcription: {transcription}"
            
            response = client.chat.completions.create(
                model=config.get("model", "gpt-3.5-turbo"),
                messages=[
                    {"role": "system", "content": config.get("system_prompt", "Eres un asistente útil.")},
                    {"role": "user", "content": formatted_transcription}
                ],
                temperature=config.get("temperature", 0.6),
                max_tokens=config.get("max_tokens", 1000),
                top_p=config.get("top_p", 1),
                frequency_penalty=config.get("frequency_penalty", 0),
                presence_penalty=config.get("presence_penalty", 0)
            )
            
            return True, response.choices[0].message.content
        except Exception as e:
            return False, f"Error al comunicarse con GPT: {str(e)}"


class GptQueryThread(QThread):
    """Hilo para enviar consultas a GPT sin bloquear la interfaz"""
    query_complete = pyqtSignal(bool, str)
    
    def __init__(self, api_key, transcription):
        super().__init__()
        self.api_key = api_key
        self.transcription = transcription
    
    def run(self):
        success, result = GptClient.send_to_gpt(self.api_key, self.transcription)
        self.query_complete.emit(success, result)
