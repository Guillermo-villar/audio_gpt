# 🎙️ audio_gpt con PyQT

**audio_gpt** es una aplicación discreta para **Windows 11** que captura el **audio interno del sistema** (como conferencias, vídeos o llamadas), lo transcribe en segundo plano utilizando la **API Whisper de OpenAI**, y gestiona el flujo de trabajo mediante un sistema **productor-consumidor** basado en colas.

> ⚠️ Este proyecto **solo funciona en Windows 11**, ya que depende de dispositivos de grabación internos como *Stereo Mix*.

---

## 🚀 Características

- ✅ Captura el audio interno del sistema (no del micrófono).
- 🧠 Transcribe automáticamente usando la API de Whisper (OpenAI).
- 🔁 Usa un sistema **asíncrono de productor-consumidor** para grabar y procesar en paralelo.
- 🪟 Aplicación discreta, pensada para ejecutarse en segundo plano en Windows.
- 💬 Transcripciones listas para ser usadas con modelos de lenguaje como GPT-4.

---

## 🖥️ Requisitos

- Windows 11
- Python 3.9 o superior
- Acceso a la API de OpenAI con créditos o plan activo
- Dispositivo de grabación tipo **Stereo Mix** (activado en el sistema)

---

## 📦 Instalación

1. Clona el repositorio:

```bash
git clone https://github.com/tu-usuario/audio_gpt.git
cd audio_gpt
```

2. Crea y activa un entorno virtual:

```bash
python -m venv venv
venv\Scripts\activate
```

3. Instala las dependencias:

```bash
pip install -r requirements.txt
```

4. Crea un archivo `.env` con tu clave de OpenAI:

```env
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

---

## 🧠 ¿Cómo funciona?

El sistema se basa en dos componentes principales:

### 🎤 Productor

- Graba fragmentos de audio del sistema cada `X` segundos.
- Coloca los fragmentos en una cola para ser transcritos.
- Funciona en segundo plano continuamente.

### 🧾 Consumidor

- Toma fragmentos de audio desde la cola.
- Llama a la **API de Whisper** de OpenAI.
- Devuelve texto limpio, listo para ser mostrado, almacenado o enviado a otro modelo (GPT, etc).

---

## 🛠️ Uso

Lanza la aplicación principal:

```bash
python main.py
```

Puedes configurar la duración de grabación, modelo Whisper (`whisper-1`, etc.), y otros parámetros desde `config.py` o como variables de entorno.

---

## 📁 Estructura del proyecto

```
audio_gpt/
├── main.py                  # Arranque del sistema productor-consumidor
├── audio_capture.py         # Grabación de audio del sistema
├── transcriber.py           # Conexión con la API de OpenAI (Whisper)
├── queue_worker.py          # Gestión de la cola de tareas
├── utils.py                 # Funciones auxiliares
├── config.py                # Configuración del sistema
├── .env                     # Tu clave API (no subir)
├── requirements.txt         # Dependencias del proyecto
└── README.md                # Este documento
```

---

## 🧪 Ejemplo de resultado

Transcripción generada desde un vídeo de conferencia:

```
"Buenos días a todos. Vamos a comenzar la presentación sobre inteligencia artificial aplicada a medicina..."
```

---

## 🔐 Seguridad

- Usa `api_key.txt` para almacenar tu clave API de OpenAI de forma segura.
- Asegúrate de que `api_key.txt` esté incluido en `.gitignore`!! Sino cualquiera tendrá acceso a tu clave de OpenAI.
- Nunca subas tu clave a GitHub o compartas públicamente tu entorno.

---

## 🧩 Ideas futuras

- Transcripción en tiempo real (streaming).
- Clasificación automática de fragmentos con GPT.
- Exportación de texto a TXT, DOCX o PDF.
- Interfaz gráfica opcional con PyQt para controlar grabación y procesamiento.

---

## 🤝 Contribuciones

¡Pull requests y sugerencias son bienvenidas!  
Este proyecto está en desarrollo activo, así que cualquier mejora, refactorización o integración es bienvenida.

---

## 📄 Licencia

Este proyecto está licenciado bajo la [MIT License](LICENSE).

---
