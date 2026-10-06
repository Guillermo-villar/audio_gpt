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

## 2. Arrancar

```powershell
python main.py
```

En la GUI: Fuente = "Loopback + micro (2 carriles)", Proveedor = "deepgram",
Modelo = "flux-general-multi". Reproduce un podcast/vídeo en inglés para
tener audio real (YouTube sirve).

## 3. Qué verificar (checklist de pruebas)

- [ ] STT en vivo: el transcript muestra líneas "Entrevistador:" en azul.
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
- [ ] Diarizar: con nova-3 + `diarize=true`, un solo stream separa
      hablantes en carriles (speaker 0/1 → Entrevistador/Tú).
- [ ] Guardar: "Guardar" crea `transcripts/transcripcion_*.txt` con todo.

Si auto-GPT no dispara con auto-GPT ON: puede ser el gate clef-flash sin
credenciales CF (cae a la heurística local, menos precisa) — revisar
cloudflare_api_key.txt / cloudflare_account_id.txt.
