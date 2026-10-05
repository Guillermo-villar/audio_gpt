# Design guidance: interview-scoped context for a live copilot

You are advising on a Windows desktop interview copilot (PySide6). Pipeline: WASAPI loopback + mic → Deepgram live (interims + finals, two labeled lanes "Entrevistador"/"Tú") → question gate → draft answer on interim (200 tokens) → review on final with rolling context. Today the context is literally `deque(maxlen=16)` but only the last 8 entries are sent — raw utterances, no structure, no filtering, no memory beyond the window.

We want to design **interview-scoped context**: per-interview material the user loads before a call — CV bullets, job description, tech stack, talking points, company notes — that the LLM uses to tailor answers (e.g. "answering as a senior backend engineer interviewing for X"). Design the smart version of this feature. Constraints: draft calls must stay under ~1.5-2s end-to-end (token budget matters), reviews can afford more; the model is gpt-6-luna (~1M ctx, $0.10/$0.50 per M short ctx).

Decide and justify:

1. **Tier structure** — what goes in every call unconditionally (pinned brief), what gets retrieved only when relevant (long docs), and what never belongs in context. Suggest concrete token budgets per tier for draft vs review.
2. **Retrieval mechanism** — for a single-user desktop app with one interview at a time: is keyword/section matching enough, or is embedding retrieval worth it? How does the model "decide to read or not" — tool call, pre-filter, or just a small always-on brief? What's simplest that actually works live?
3. **Rolling transcript replacement** — the fixed 8-utterance FIFO is weak for hour-long calls: questions, requirements and decisions scroll off. Design the replacement: pinned facts extracted live? rolling summary? topic segmentation? What should NEVER roll off (stated requirements, scale constraints, the user's own previous answers)?
4. **Authoring UX** — how does the user build the brief? Paste job description → auto-extract structured fields? Free text? Per-company saved profiles? What does the UI need (one textarea vs structured fields vs file drop)?
5. **Answer tailoring** — how should the brief shape answers (voice, seniority, examples the user can claim) without the model fabricating experience the user doesn't have?
6. **Update loop** — should the copilot append facts to the brief mid-call (e.g. interviewer said "we use Postgres at 10M rps" → pinned)? Who edits it: heuristics, a small model, the user?

Output: a concrete design spec — tier definitions with token budgets, the context-payload layout sent to the model (exact structure), the minimal UI, and what to build first vs later. Be opinionated; prefer the simplest thing that works live over infrastructure. Cite tradeoffs honestly (retrieval adds latency + failure modes).
