# Build + test de audio_gpt en local (Windows)

Trabaja en el repo `audio_gpt` en la rama `devin/1791153246-modernize-stt-llm`.

## 1. Setup

```powershell
git checkout devin/1791153246-modernize-stt-llm; git pull
python -m venv venv; .\venv\Scripts\Activate.ps1
pip install -r requirements.txt
# credenciales (NO commitear, están en .gitignore):
"<DEEPGRAM_KEY>"      | Out-File -NoNewline deepgram_api_key.txt -Encoding ascii
"<CF_TOKEN>"          | Out-File -NoNewline cloudflare_api_key.txt -Encoding ascii
"<CF_ACCOUNT_ID>"     | Out-File -NoNewline cloudflare_account_id.txt -Encoding ascii
"<OPENAI_KEY>"        | Out-File -NoNewline api_key.txt -Encoding ascii   # opcional
```

Si hay cambios locales sin commitear en la working tree, `git stash` antes
del pull o se pisarán.

## 2. Arrancar — config para llamada de 3+ personas ES/EN

```powershell
python main.py
```

En la GUI: **Fuente = "Loopback + micro (2 carriles)"**, **Proveedor =
"deepgram"**, **Modelo = "nova-3"**, idioma `multi` (ES↔EN code-switching
nativo) y marca **"Diarizar"**: el carril loopback separa a los 3
entrevistadores en speakers 0/1/2. El micro (tu voz) va por Flux aparte.

Para probar sin call real: reproduce una entrevista/podcast en YouTube
con varios hablantes.

## 3. Qué verificar (checklist de pruebas)

- [ ] UI clara: toda la app en tema claro (fondo claro, campos blancos,
      texto oscuro) aunque Windows esté en modo oscuro.
- [ ] STT en vivo: DOS paneles — "Entrevistador" (voces de la llamada)
      a la izquierda y "Tú (micro)" (tu voz) a la derecha.
- [ ] Diarización: con varias voces, aparecen etiquetas de speaker distintas
      dentro del panel Entrevistador.
- [ ] Modo compacto: `Ctrl+I` abre el overlay siempre encima (arrastrable,
      doble-clic oculta). Repítelo para cerrar.
- [ ] Hotkeys con otra ventana enfocada (ej. el navegador):
      `Ctrl+M` conmuta auto-GPT (status bar dice ON/OFF),
      `Ctrl+Q` responde la última intervención,
      `Alt+C` copia la última respuesta al portapapeles,
      `Alt+T` start/stop de la captura.
- [ ] Auto-GPT ON: al oír una pregunta sale un "[Borrador]" rápido y luego
      una "[Revisión]" más completa en el panel de respuestas.
- [ ] Brief: escribe algo en "Brief de la entrevista" y comprueba que las
      respuestas se adaptan a ese contexto (menciona tu puesto/experiencia).
- [ ] Fijar: selecciona una línea del transcript, pulsa Fijar; en la
      siguiente respuesta el modelo usa ese dato.
- [ ] Guardar: "Guardar" crea `transcripts/transcripcion_*.txt` con todo.

Si auto-GPT no dispara con auto-GPT ON: puede ser el gate clef-flash sin
credenciales CF (cae a la heurística local, menos precisa) — revisar
cloudflare_api_key.txt / cloudflare_account_id.txt.

## 4. meet_split — audio por asistente en Google Meet (PoC)

Separa cada asistente de una Meet en un canal de audio propio (Chrome
extension que engancha los tracks WebRTC) → un stream Deepgram por
asistente.

```powershell
pip install websockets          # ya está en requirements.txt
$env:DEEPGRAM_API_KEY = "<DEEPGRAM_KEY>"
python meet_split\server.py     # escucha ws://127.0.0.1:8765
```

Luego en Chrome: `chrome://extensions` → modo desarrollador →
"Cargar descomprimida" → carpeta `meet_split/` → únete a la llamada.
En la consola del server aparecerá `[meet-split] track Meet-S1/S2/...`
y transcripts `[Meet-Sn] (FINAL) ...` por canal. WAVs por canal en
`meet_split/out/`.

**Caveat**: Meet puede agregar varios asistentes en un mismo "virtual
stream" del SFU — Meet-Sn ≈ canal, no garantía 1:1 persona. Si no salen
tracks, plan B = la config del punto 2 (nova-3+multi+diarize sobre el
loopback mezclado).
