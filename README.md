# 🎙️ audio_gpt

Aplicación de escritorio para **Windows** que graba el **audio del sistema** (reuniones, vídeos, llamadas) o el micrófono, lo **transcribe en directo** y, opcionalmente, envía el transcript a un **LLM** cuando se lo pides (`Ctrl+Q`) — pensada como copiloto para entrevistas técnicas, clases y reuniones.

## ✨ Qué hay de nuevo (v2)

- **Sin VB-Cable**: captura el audio del sistema por **loopback WASAPI** nativo (con `soundcard`). VB-Cable sigue disponible como respaldo.
- **VAD real**: segmenta por turnos de habla con `webrtcvad` en lugar de cortes fijos — no corta palabras y no gasta API en silencio.
- **Modelos actuales**: `whisper-1` → `gpt-4o-transcribe` (o `gpt-4o-mini-transcribe`, más barato, o `gpt-4o-transcribe-diarize` con **etiquetas de hablante**).
- **Streaming real**: proveedor *OpenAI Realtime* — WebSocket a la Realtime API con VAD en servidor; la transcripción llega por turnos casi en directo.
- **Alternativas gratis/baratas**: *Groq* (`whisper-large-v3-turbo`, ~$0.04/h) y *local* con `faster-whisper` (offline, privado, sin coste).
- **GPT moderno**: la respuesta usa la **Responses API** con `gpt-6-luna` por defecto (edítalo en `gpt_config.json`; alternativas: `gpt-5-mini`, `gpt-6.1-sol`). Los modelos de razonamiento no aceptan `temperature`, así que esa opción se ignora de forma segura.
- **Copiloto bajo orden**: con `Ctrl+Q` (o `Alt+G` / botón) una respuesta rápida de Luna y, debajo, los detalles de Sol; sin respuestas automáticas ni detección de preguntas.
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
4. Las respuestas se piden tú: **`Ctrl+Q`** (o `Alt+G` / botón «Enviar a GPT») responde a la última intervención del entrevistador; no hay respuestas automáticas ni detección de preguntas. El motor puede ser *API OpenAI*, *Cloudflare AI (créditos CF)* o *Codex CLI (ChatGPT sub)* — este último gasta la cuota de tu suscripción en vez de la API (requiere `npm i -g @openai/codex` + `codex login`).
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

## Rama: primera llamada exploratoria (recruiter / encaje)

Esta rama (`devin/recruiter-call-copilot`) adapta el copiloto a una llamada
de 30 min sin prueba técnica (primer contacto con una empresa). El system
design vive en `devin/system-design-copilot`; el resto de la app (overlay
Ctrl+I, cascada Luna→Sol, caché, mock) es el mismo.

- `prompts.SYSTEM_PROMPT` describe las fases de la llamada (presentación,
  «cuéntame de ti», experiencia, motivación, logística, cierre) y obliga a
  hablar en primera persona, sin inventar métricas ni experiencia.
- `prompts.DEFAULT_BRIEF` lleva el contexto del candidato, los objetivos de
  la llamada por prioridad, las líneas rojas y los datos investigados de la
  empresa. Se carga en «Brief de la entrevista» cuando el campo está vacío
  (si ya tienes un brief guardado, bórralo y reinicia) y forma parte del
  prefijo cacheado.
- Luna (Ctrl+Q): **Di ahora** en tono de conversación + 2-4 viñetas, con
  **Pregunta:** cuando toca preguntar. Sol añade `### ⚠ Ojo` (línea roja,
  dato inventado, contradicción), `### Matiz`, `### Datos` (empresa),
  `### Pregunta` (la siguiente, redactada) y `### Pendiente` (objetivos sin
  cubrir).
- Keyterms del STT: nombres propios de la llamada (Orbio, Aida, AXA, UC3M,
  FDE, RAG…) en vez de la jerga de system design.
- Mock sin coste: `$env:AUDIO_GPT_MOCK="fast"; .\venv\Scripts\python main.py`
  reproduce la llamada con Aida (pitch, «¿cuánta gente lo usa?», salario,
  oficina, siguiente paso) con respuestas enlatadas en el nuevo formato.

## Uso durante la llamada (modo copiloto)

- **Panel oculto** (`Ctrl+I`, único control de visibilidad): panel oscuro,
  sin foco y siempre encima, con el transcript reciente por carriles y las
  respuestas en una sola tarjeta Markdown: una respuesta de Luna reemplaza la
  anterior al enviar una nueva. Al pulsar `Ctrl+Q` reserva de golpe la altura
  de lectura (un 60 % de la pantalla o la de la última respuesta completa)
  para que el texto de Luna se lea directo sin esperar a que el panel crezca;
  mientras llega texto solo crece, sin animación, hasta un 92 % de la
  pantalla (85 % con una sola columna) y se ensancha para código.
  `Ctrl+flechas` lo mueve, `Ctrl+±` ajusta su opacidad y también se arrastra.
- **Hotkeys globales** (2 teclas, funcionan con otra app enfocada - elegidas
  para no interferir con el navegador; las teclas disparadas se consumen y no
  llegan al navegador). Cada comando deja constancia visible (flash en el
  panel o toast flotante):
  `Ctrl+Q` responder la última intervención del entrevistador ·
  `Alt+S` sustituir la respuesta visible por una versión completa de gpt-6.1-sol ·
  `Alt+G` enviar el transcript a GPT · `Alt+W` dibujar la arquitectura ·
  `Ctrl+I` panel · `Alt+T` start/stop transcripción.
  Las respuestas de GPT nunca se copian solas al portapapeles.
- **Formato de respuestas**: todos los motores reciben instrucciones para
  responder en Markdown breve. `Alt+S` sustituye la respuesta visible por una
  versión completa de Sol; la anterior queda plegada arriba en una fila
  «Anterior» y se puede desplegar para consultar su Markdown. Otra respuesta
  raíz sustituye la tarjeta y su historial.
- **Privacidad visual**: activa «Invisible al compartir pantalla / grabar»
  para excluir el panel, las confirmaciones y la ventana principal de las
  capturas compatibles de Windows.
- **Solo bajo orden**: no hay detección de preguntas ni respuestas
  automáticas. Una respuesta se genera únicamente con `Ctrl+Q`, `Alt+G` o el
  botón «Enviar a GPT» (y `Alt+S` / `Alt+W` / «Dibujar» sobre lo que ya hay).
  Con «Pre-cachear transcript» activo el transcript SÍ se envía a OpenAI
  (sin generar nada) mientras la transcripción continua está en marcha, y
  con «Segunda transcripción» el audio de los turnos del entrevistador
  también se envía a OpenAI.
- **Brief de la entrevista**: puesto, empresa, experiencia aprobada y
  límites — va en el system prompt de cada llamada (nunca fabrica
  experiencia que el brief no respalde).
- **Fijar**: selecciona texto del transcript y fíjalo — requisitos y
  decisiones entran en el contexto permanente.

## Copiloto de system design (v3)

Pensado para entrevistas de **system design**: la respuesta llega en dos
tiempos y todo se calcula sobre un prefijo de prompt estable para aprovechar
la caché de OpenAI.

- **Flujo de `Ctrl+Q` (Luna → Sol)**: `gpt-6-luna` responde primero, rápido,
  con «**Di ahora:**» y 3-5 viñetas. Cuando termina, `gpt-6.1-sol` añade
  debajo (separado por una línea fina) sus *detalles*: `⚠ Corrección` (solo si
  la respuesta rápida se equivocó), `Detalles`, `Números` (estimaciones con la
  cuenta visible), `Diagrama` y `Si te preguntan…`. Cada cabecera muestra
  tiempo, primer token y % de caché. Una nueva pregunta cancela el detalle
  pendiente de la anterior; `Alt+S` sigue sustituyendo la versión visible.
- **Diagramas nativos**: los bloques ```` ```mermaid ```` (subconjunto de
  `flowchart`/`graph`: formas, aristas con etiqueta, subgrafos) se dibujan en
  el panel con colores por rol (caché, cola, BD, cliente, infraestructura…),
  sin navegador ni dependencias. Mientras llegan por streaming se muestra
  «✎ dibujando diagrama…». `Alt+W` (o el botón **Dibujar**) pide a Luna el
  diagrama de la arquitectura definida hasta ahora en la entrevista.
- **Pre-cacheo (prewarm)**: con «Pre-cachear transcript (Luna/Sol)» y la
  transcripción continua en marcha, el transcript se envía a OpenAI con
  `prewarm` de la caché de prompts: **nunca genera ni muestra nada**, solo
  hace que `Ctrl+Q` arranque ~4× más rápido. Un ping cada 30 s mantiene viva
  la conexión (no envía transcript) aunque el pre-cacheo esté desactivado.
  Los pre-cacheos van sin Fast mode (`prewarm_service_tier: "auto"`; no
  corren prisa y así no pagan el recargo) y una respuesta real cuenta como
  pre-cacheo del mismo transcript, así que no se recalienta justo después de
  preguntar. Si pulsas `Ctrl+Q` con un pre-cacheo aún en vuelo, la pregunta
  **no espera**: sale al momento con el transcript actual y OpenAI reutiliza
  el prefijo cacheado más largo que coincida (lo estable + las líneas ya
  calentadas); solo las líneas nuevas van sin caché. Un pre-cacheo viejo
  nunca cambia la respuesta.
- **Modo simulado sin gastar tokens**: `AUDIO_GPT_MOCK=fast python main.py`
  arranca la app con un backend local que imita los tiempos de
  `gpt-6-luna`/`gpt-6.1-sol` (primer token, deltas, búsqueda web, caché,
  pre-cacheo, árbitro) y una entrevista guionizada en el transcript; sirve
  para probar `Ctrl+Q`/`Alt+S`/`Ctrl+I` sin API key. Perfiles: `fast`,
  `slow` (latencias degradadas), `flaky` (errores 429/500, salidas vacías,
  Sol caído) y `burst` (deltas de un carácter / una sola ráfaga).
  `AUDIO_GPT_MOCK_SPEED=3` lo acelera 3×. En PowerShell:
  `$env:AUDIO_GPT_MOCK="fast"; .\venv\Scripts\python main.py`.
- **Segunda transcripción + árbitro**: con Deepgram streaming, cada turno
  final del entrevistador se vuelve a transcribir en segundo plano con
  `gpt-transcribe` (audio de un búfer circular alineado con los tiempos de
  Deepgram). Al pedir una respuesta, Luna recibe ya las líneas que difieren y
  un árbitro rápido (Luna, salida JSON) concilia ambas transcripciones; Sol
  responde a la pregunta verificada. La tarjeta lo indica: `✓ transcripción
  verificada`, `✓ verificada: «…»` o `⚠ Luna pudo oír mal — Sol responde a:
  «…»` (el tooltip lista las correcciones). Si el árbitro tarda más de
  `verify_deadline_s`, Sol arranca igualmente sin verificar. Se desactiva con
  la casilla «Segunda transcripción (gpt-transcribe)».
- **Términos clave de system design**: se añaden automáticamente a los
  «Términos clave» de Deepgram (nova-3 y Flux) y a las `keywords` de
  `gpt-transcribe` (`sd_keyterms`).
- **Telemetría local**: `logs/llm_calls.jsonl` guarda una línea por llamada
  (tipo, modelo, estado, latencias y tokens/caché). Nunca contiene prompts ni
  respuestas; está en `.gitignore`.
- **Claves de `gpt_config.json`** (todas opcionales; los valores por defecto
  están en `api_client.DEFAULT_GPT_CONFIG` y los textos en `prompts.py`):
  `detail_enabled`, `detail_model`, `detail_reasoning_effort`,
  `detail_max_tokens`, `detail_verbosity`, `fast_verbosity`, `prewarm`,
  `sd_keyterms`, `verify_enabled`, `verify_stt_model`, `arbiter_model`,
  `verify_wait_s`, `verify_deadline_s`, `diagram_reasoning_effort`,
  `diagram_max_tokens`, `prewarm_service_tier`.
