# AGENTS.md

Live interview copilot for Windows (PySide6). Deepgram streams the call
transcript; Luna answers fast, Sol adds details and diagrams. Focus: system
design interviews.

## Run

- Python: `C:\Users\guill\Documents\audio_gpt-modernized\venv\Scripts\python.exe`
  (PySide6, openai, python-dotenv; do not install anything else).
- Run from the worktree root so `.env`, `api_key.txt` and `gpt_config.json`
  load: `python main.py`.

## Tests

- `python -m unittest discover -s tests -v` from the worktree root. No pytest.
- Tests run with `QT_QPA_PLATFORM=offscreen` (set by the test modules). That
  platform has no fonts: for screenshots/`grab()` use `QT_QPA_PLATFORM=windows`.
- `python markdown_smoke.py` must keep passing.
- Zero-cost backend: `mock_llm.py` (+ `mock_answers.py`, `mock_feed.py`)
  replaces `llm.stream/prewarm/ping`, the arbiter and the realtime feed
  with a simulator that mimics gpt-6-luna/gpt-6.1-sol timing, the explicit
  prompt-cache prefix rules and failure modes. Run the app with
  `AUDIO_GPT_MOCK=fast|slow|flaky|burst python main.py`
  (`AUDIO_GPT_MOCK_SPEED=N` speeds everything up N×). Never add a real
  API call to the mock path. `tests/test_meeting_pressure.py` drives a
  real `WhisperApp` against it (hotkey races, errors, overlay toggles,
  prewarm-vs-question); `tests/test_mock_backend.py` pins the simulator.
- `tests/test_app_flow.py` builds a real `WhisperApp` with the keyboard hook
  and `SETTINGS_PATH` patched, and fake `llm.stream`/arbiter. Never touch the
  real `settings.json` and never call `WhisperApp.closeEvent` in tests (it
  deletes `temp_audio/`).

## Module map

- `prompts.py` — every prompt text (system, modes, headers, arbiter, keyterms).
- `api_client.py` — API keys, `DEFAULT_GPT_CONFIG`, legacy engines (Cloudflare,
  Codex), question gate, `brief_block`.
- `llm.py` — OpenAI Responses requests with explicit prompt cache: request
  builders, streaming, prewarm, ping, `logs/llm_calls.jsonl` telemetry.
- `copilot.py` — pure logic: question window, dynamic prompt tails, `Cascade`
  (Luna → Sol gating), `PrewarmScheduler`, keyterm merge.
- `verify.py` — second transcription (`SecondEar`, gpt-transcribe) and the
  Luna arbiter (`reconcile`).
- `audio_ring.py` — PCM16 ring buffer + WAV helper for turn re-transcription.
- `transcriber.py` — STT engines; `DeepgramRealtime` parses events and, with
  `with_meta=True`, passes turn timestamps/words.
- `capture.py`, `vad.py` — WASAPI loopback capture and voice segmentation.
- `diagram.py` — Mermaid flowchart split/parse/layout (no Qt);
  `diagram_view.py` — Qt widgets that paint it.
- `gui.py` — main window, overlay (Ctrl+I), answer cards, hotkeys, threads.
- `tests/` — unittest suite.

## Rules

- Prompts live only in `prompts.py` (config keys in `gpt_config.json` may
  override by name). Do not inline prompt text elsewhere.
- Never print, log or copy credentials. `settings.json`, `*_api_key.txt`,
  `api_key.txt`, `.env` and `logs/` are gitignored; keep it that way.
- Telemetry logs metrics only (no prompt or answer text).
- The overlay is shown/hidden only by Ctrl+I; nothing auto-shows it.
- No automatic answering or question detection; answers only on explicit
  order (Ctrl+Q / Alt+G / button). Prewarm and the keep-alive ping never
  generate output.
- Keep diffs minimal, add new dependencies only with approval, and do not
  commit or push unless asked.
