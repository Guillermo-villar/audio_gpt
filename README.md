# 🎙️ audio_gpt

Aplicación de escritorio para **Windows** que graba el **audio del sistema** (reuniones, vídeos, llamadas) o el micrófono, lo **transcribe en directo** y, opcionalmente, envía cada fragmento a un **LLM** para obtener respuestas automáticas — pensada como copiloto para entrevistas técnicas, clases y reuniones.

## ✨ Qué hay de nuevo (v2)

- **Sin VB-Cable**: captura el audio del sistema por **loopback WASAPI** nativo (con `soundcard`). VB-Cable sigue disponible como respaldo.
- **VAD real**: segmenta por turnos de habla con `webrtcvad` en lugar de cortes fijos — no corta palabras y no gasta API en silencio.
- **Modelos actuales**: `whisper-1` → `gpt-4o-transcribe` (o `gpt-4o-mini-transcribe`, más barato, o `gpt-4o-transcribe-diarize` con **etiquetas de hablante**).
- **Streaming real**: proveedor *OpenAI Realtime* — WebSocket a la Realtime API con VAD en servidor; la transcripción llega por turnos casi en directo.
- **Alternativas gratis/baratas**: *Groq* (`whisper-large-v3-turbo`, ~$0.04/h) y *local* con `faster-whisper` (offline, privado, sin coste).
- **GPT moderno**: la respuesta usa la **Responses API** con `gpt-6-luna` por defecto (edítalo en `gpt_config.json`; alternativas: `gpt-5-mini`, `gpt-6.1-sol`). Los modelos de razonamiento no aceptan `temperature`, así que esa opción se ignora de forma segura.
- **Copiloto en dos pasadas**: con streaming, un *borrador* responde sobre el texto parcial mientras la persona sigue hablando, y una *revisión* con más razonamiento y contexto de la conversación aterriza al cerrar el turno.
- **Puerta de preguntas**: una heurística local filtra muletillas y charla — solo lo que suena a pregunta/encargo técnico llega al LLM.
- **Deepgram**: *Flux* (~20 ms fin de turno) o *Nova-3* (~1.6% WER, `diarize` para etiquetar voces en llamadas de panel y `keyterm` para reforzar jerga técnica).
- Migrado de **PyQt5 a PySide6** (Qt6: mejor soporte de HiDPI y licencia LGPL).

## 🚀 Requisitos

- Windows 10/11
- Python 3.9+ (probado con 3.13)
- Una API key según el proveedor elegido:
  - **OpenAI**: <https://platform.openai.com/api-keys> — pago por uso (~$0.003–0.006/min de transcripción). *Nota: la suscripción ChatGPT Plus/Pro no incluye uso de API; son productos separados.*
  - **Deepgram**: <https://console.deepgram.com/> — streaming Flux (~20 ms fin de turno) / Nova-3 (~250 ms, ~1.6% WER), ~$0.006–0.008/min.
  - **Groq**: <https://console.groq.com/keys> — casi gratis.
  - **Local**: sin clave; instala `faster-whisper` y el modelo se descarga solo (~1.6 GB para `large-v3-turbo`).
  - Para GPT también vale **Cloudflare Workers AI**: sirve `openai/gpt-6-luna` con endpoint compatible — define `CLOUDFLARE_API_TOKEN` y `CLOUDFLARE_ACCOUNT_ID` (o `cloudflare_api_key.txt` / `cloudflare_account_id.txt`).

## 📦 Instalación

```bash
git clone https://github.com/Guillermo-villar/audio_gpt.git
cd audio_gpt
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
# opcional, para transcripción local gratuita:
pip install faster-whisper
```

Crea un `.env` con tu clave (o déjalo y la app te la pedirá la primera vez):

```env
OPENAI_API_KEY=sk-...
GROQ_API_KEY=gsk_...        # solo si usas Groq
DEEPGRAM_API_KEY=...        # solo si usas Deepgram
```

## ▶️ Uso

```bash
python main.py
```

1. **Fuente**: «Audio del sistema (loopback WASAPI)» — graba lo que suena sin tocar la configuración de Windows. Alternativas: micrófono, **«Loopback + micro (2 carriles)»** — captura dos canales independientes y etiqueta `Entrevistador:` (loopback) y `Tú:` (micro) como hace Granola — o VB-Cable.
2. **Proveedor/modelo**: OpenAI `gpt-4o-transcribe` recomendado; `…-diarize` para reuniones con varios hablantes; *Deepgram* (`flux-general-multi`, el más rápido del mercado ~20 ms fin de turno; `nova-3-multilingual` si prefieres precisión ~1.6% WER) para latencia mínima; *OpenAI Realtime* (`gpt-live-transcribe`) como opción OpenAI-native; *Groq* o *local* para gastar (casi) nada. El modo dúo funciona con OpenAI/Groq/local/Deepgram — con Deepgram cada carril es un WebSocket propio — (no con OpenAI Realtime).
3. Pulsa **INICIAR TRANSCRIPCIÓN CONTINUA**. La VAD detecta la voz y cada fragmento se transcribe y aparece en pantalla.
4. Activa **«Responder con GPT automáticamente»**: la puerta de preguntas decide qué intervenciones se responden; con Deepgram verás un *[Borrador]* al vuelo y una *[Revisión]* al cerrar el turno. El motor puede ser *API OpenAI*, *Cloudflare AI (créditos CF)* o *Codex CLI (ChatGPT sub)* — este último gasta la cuota de tu suscripción en vez de la API (requiere `npm i -g @openai/codex` + `codex login`).
5. **Opciones Deepgram**: «Diarizar (panel)» etiqueta `<S0>/<S1>` dentro de un carril (nova-3); «Términos clave» refuerza vocabulario técnico.
6. **Grabar** (duración fija) + **Transcribir grabación** sigue disponible para uso puntual.

## 📁 Estructura

```
audio_gpt/
├── main.py           # Punto de entrada
├── gui.py            # Interfaz PySide6 + hilos de captura/transcripción
├── capture.py        # Loopback WASAPI (soundcard) y dispositivos (sounddevice)
├── vad.py            # Segmentación por voz (webrtcvad)
├── transcriber.py    # Motores: OpenAI, Realtime WS, Deepgram WS/REST, Groq, faster-whisper local
├── api_client.py     # API keys, Responses API (GPT), compatibilidad
├── recorder.py       # Diagnóstico de audio por línea de comandos
├── gpt_config.json   # Modelo GPT, prompt de sistema, esfuerzo de razonamiento
└── settings.json     # Última configuración elegida (autogenerado)
```

## 🔐 Notas

- Las claves se guardan en `api_key.txt` / `<proveedor>_api_key.txt` (en `.gitignore`) o vía variables de entorno.
- `gpt_config.json` controla el modelo y el prompt; `"reasoning_effort": "low"` da respuestas rápidas y baratas — súbelo a `"medium"` si necesitas más calidad.

## 📄 Licencia

MIT.
