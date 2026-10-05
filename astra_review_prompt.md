# Deep review: audio_gpt as a live-call copilot

You are reviewing a Windows desktop app (PySide6) that acts as a real-time interview copilot. Read the ENTIRE codebase first — every file, fully — before writing anything.

**Repo/branch**: `audio_gpt`, branch `devin/1791153246-modernize-stt-llm` (the PR branch — NOT master).

**What it does today**
- `capture.py`: WASAPI loopback captures call audio (lane "Entrevistador"), mic captures the user (lane "Tú"). Two independent 16kHz PCM16 lanes.
- `vad.py`: webrtcvad segmenter per lane.
- `transcriber.py`: STT engines — Deepgram WS (flux-general-multi on the Flux v2 protocol `TurnInfo`, nova-3 + `language=multi`, optional `diarize` + `keyterm`), OpenAI realtime WS, OpenAI REST file, Groq, local faster-whisper.
- `api_client.py`: LLM engines — OpenAI Responses API (gpt-6-luna, effort + service_tier), Cloudflare Workers AI (`@cf/` models via chat.completions, third-party via responses), Codex CLI. `looks_like_question()` keyword gate. Rolling context deque (~last 16 labeled utterances).
- `gui.py`: PySide6 panel. Pipeline: interim transcripts can fire a capped 200-token **draft** answer mid-turn (`[Borrador]`); the final transcript fires a deeper **review** answer with full context (`[Revisión]`). "Tú" lane never triggers the LLM.
- `eval_stt.py`: offline harness streaming recorded audio through the real Deepgram path.

**How to judge it**
Think like a power user who lives in Granola / Cluely / a real copilot during a live interview. Be opinionated, cite file+line, rank everything by impact-per-effort.

1. **Latency budget** — walk the real timeline from "interviewer starts asking" to "user reads the draft". Where does perceived latency actually die? Is draft-on-interim firing at the right moment (too early = wrong question, too late = useless)?
2. **Failure modes** — when the draft answers a half-formed question, what does the user see and how confusing is the correction? When the gate false-negatives, what's lost? When it false-positives, what's the cost?
3. **Threading/state races** — `_draft_state` dedupe, `_ctx` deque semantics under interleaved lanes, draft vs review overwrite/ordering, engine switching mid-call.
4. **Context strategy** — is the rolling-16 window right? Are follow-ups ("and how would you scale that?") resolvable? What about hour-long calls?
5. **Usability under pressure** — what does the UI show at each moment? Can the user glance-and-go mid-question? What's one thing a Granola user would call missing within 30 seconds?
6. **Missing features** — rank the highest-leverage additions for live-call utility specifically.

**Output format** (structured, no fluff):
- `verdict`: 2-3 sentences, honest.
- `bugs`: [{severity, file:line, issue}] — real defects, ranked.
- `architecture_assessment`, `usability_assessment`: strengths briefly, weaknesses deeply.
- `missing_features_ranked`: [{feature, live_call_impact, effort}].
- `top_recommendations`: max 8, ranked by impact-per-effort.

No code changes. Review only.
