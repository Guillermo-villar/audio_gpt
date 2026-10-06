# meet_split — audio por asistente en Google Meet (PoC)

Separa el audio de cada asistente de una llamada de Google Meet **antes**
de que el navegador los mezcle. Cada asistente remoto llega a Chrome como
un `MediaStreamTrack` distinto en el `RTCPeerConnection`; la extensión
engancha cada track, extrae PCM 16 kHz mono y lo envía por WebSocket a
`server.py`, que abre **una conexión de Deepgram por asistente** y además
graba un WAV por canal en `out/`.

## Estado

Probado end-to-end con una página WebRTC sintética de 2 "asistentes":
dos streams de Deepgram en paralelo transcribiendo voces distintas sin
contaminación. Limitación conocida de la prueba: en una VM sin tarjeta
de sonido los tracks *remotos* rinden silencio (los locales no) — en un
PC real con audio esto no ocurre.

## Uso en tu PC

1. `pip install websockets` (ya está en requirements.txt) y define
   `DEEPGRAM_API_KEY` (o déjalo sin definir para solo grabar WAVs).
2. `python meet_split/server.py` → escucha en `ws://127.0.0.1:8765`.
3. Chrome → `chrome://extensions` → "Cargar descomprimida" → carpeta
   `meet_split/`.
4. Entra a la llamada en meet.google.com. Por cada asistente verás en la
   consola `[meet-split] track Meet-S1 = ...` y en el servidor los
   transcripts `[Meet-Sn] (FINAL) ...` por separado.

## Notas

- El hook (`injected.js`) envuelve `RTCPeerConnection` antes de que Meet
  lo use (`document_start`). Los tracks remotos pueden llegar sin
  `e.streams` — no se exige.
- CAVEAT (investigación EXA 2026-10): Google Meet NO garantiza un track
  por asistente — su SFU puede agregar varios participantes en un mismo
  "virtual stream" y el hablante real solo se identifica por CSRC en el
  RTP. Es decir: Meet-Sn ≈ "canal de audio", no "persona". Para
  atribución a nombre real, el camino fiable es híbrido: captions DOM de
  Meet (llevan nombre del hablante) cruzadas por tiempo con nuestros
  streams — igual que documentan Vexa/Orbit/Kuali.
- `test_page.html` es la "Meet sintética" local: dos PCs en loopback con
  voces reales en loop. Sírvelo con cualquier servidor estático
  (`python -m http.server`) para reproducir la prueba.
- Próximo paso natural: que `gui.py` consuma los streams como carriles
  extra (por ejemplo `[Entrevistador S2]`) en vez del loopback mezclado.
