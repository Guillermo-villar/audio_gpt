"""Segunda transcripción (gpt-transcribe) + árbitro Luna. Sin Qt."""

import difflib
import json
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait

import audio_ring
import llm
import prompts

_TAG_RE = re.compile(r"<S\d+>")


def stt_languages(code):
    if not code:
        return None
    if code == "en":
        return ["en"]
    return [code, "en"]


def sanitize_keywords(terms, limit=40):
    seen = set()
    out = []
    for term in terms or []:
        term = (term or "").strip()
        key = term.casefold()
        if not term or key in seen:
            continue
        if any(ch in term for ch in "<>\r\n"):
            continue
        seen.add(key)
        out.append(term)
        if len(out) >= limit:
            break
    return out


def mark_low_confidence(text, words, threshold=0.6):
    if not words:
        return text
    parts = []
    for w in words:
        word = w.get("word", "")
        conf = w.get("confidence")
        parts.append(f"[{word}?]" if conf is not None and conf < threshold
                     else word)
    return " ".join(parts)


def normalize(text):
    text = _TAG_RE.sub(" ", text or "")
    text = unicodedata.normalize("NFC", text).casefold()
    text = "".join(
        " " if (unicodedata.category(ch)[0] in "PSC" or ch == "_") else ch
        for ch in text)
    return re.sub(r"\s+", " ", text).strip()


def equivalent(a, b):
    ta, tb = normalize(a).split(), normalize(b).split()
    if ta == tb:
        return True
    matcher = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace":
            return False
        at_start = i1 == 0 and j1 == 0
        at_end = i2 == len(ta) and j2 == len(tb)
        span = (i2 - i1) if tag == "delete" else (j2 - j1)
        if not ((at_start or at_end) and span <= 3):
            return False
    return True


class SecondEar:
    def __init__(self, api_key, model="gpt-transcribe", keywords=(),
                 languages=None, max_workers=2, min_seconds=0.6,
                 transcribe=None):
        self.api_key = api_key
        self.model = model
        self.keywords = list(keywords)
        self.languages = languages
        self.min_seconds = min_seconds
        self._transcribe = transcribe or self._default_transcribe
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        self._lock = threading.Lock()
        self._gen = 0
        self._results = {}
        self._futures = {}

    def _default_transcribe(self, wav, prompt):
        extra = {}
        if self.keywords:
            extra["keywords"] = list(self.keywords)
        if self.languages is not None:
            extra["languages"] = self.languages
        client = llm.get_client(self.api_key).with_options(timeout=10)
        return client.audio.transcriptions.create(
            model=self.model, file=("turn.wav", wav, "audio/wav"),
            prompt=prompt, **extra).text

    def has(self, utt_id):
        with self._lock:
            return utt_id in self._futures

    def submit(self, utt_id, ring, meta, context_text):
        if not meta:
            return False
        start, end = meta["start"], meta["end"]
        words = meta.get("words") or []
        if (words and words[0].get("start") is not None
                and words[-1].get("end") is not None):
            start, end = words[0]["start"], words[-1]["end"]
        if end - start < self.min_seconds:
            return False
        pcm = ring.slice(start, end, pad_s=0.12)
        if pcm is None:
            return False
        wav = audio_ring.wav_bytes(pcm, ring.sample_rate)
        prompt = prompts.VERIFY_STT_PROMPT.format(
            context=(context_text or "")[-500:])
        with self._lock:
            gen = self._gen
            if utt_id in self._futures:
                return False
            self._futures[utt_id] = self._pool.submit(
                self._run, gen, utt_id, wav, prompt)
        return True

    def _run(self, gen, utt_id, wav, prompt):
        started = time.monotonic()
        text = None
        error = ""
        try:
            text = (self._transcribe(wav, prompt) or "").strip() or None
        except Exception as e:
            error = str(e) or type(e).__name__
        llm.log_call(llm.CallResult(
            ok=text is not None, error=error, status="completed" if text
            else "failed", model=self.model, kind="second_ear",
            total=time.monotonic() - started))
        with self._lock:
            if gen == self._gen:
                self._results[utt_id] = text
        return text

    def result(self, utt_id):
        with self._lock:
            return self._results.get(utt_id)

    def pending(self, utt_id):
        with self._lock:
            future = self._futures.get(utt_id)
        return future is not None and not future.done()

    def wait(self, utt_ids, timeout):
        with self._lock:
            futures = [self._futures[i] for i in utt_ids
                       if i in self._futures]
        futures = [f for f in futures if not f.done()]
        if futures:
            futures_wait(futures, timeout=timeout)

    def reset(self):
        with self._lock:
            self._gen += 1
            self._results.clear()
            for future in self._futures.values():
                future.cancel()
            self._futures.clear()

    def shutdown(self):
        with self._lock:
            self._gen += 1
        self._pool.shutdown(wait=False, cancel_futures=True)


def arbiter_input(pairs, context_lines):
    return (
        prompts.ARBITER_CONTEXT_HEADER + "\n"
        + ("\n".join(context_lines) or "(ninguno)") + "\n\n"
        + prompts.ARBITER_ITEMS_HEADER + "\n"
        + "\n".join(
            f"[{i}] A: {p['a']}\n    B: {p['b'] or prompts.ARBITER_NO_B}"
            for i, p in enumerate(pairs, 1)))


def call_arbiter(api_key, config, text):
    started = time.monotonic()
    result = llm.CallResult(model=config["arbiter_model"], kind="arbiter")
    kwargs = {
        "model": config["arbiter_model"],
        "instructions": config["arbiter_prompt"],
        "input": text,
        "reasoning": {"effort": "none"},
        "text": {"format": {
            "type": "json_schema", "name": "verified_question",
            "schema": prompts.ARBITER_SCHEMA, "strict": True}},
        "max_output_tokens": 400,
    }
    tier = config.get("service_tier")
    if tier and tier != "auto":
        kwargs["service_tier"] = tier
    try:
        client = llm.get_client(api_key).with_options(timeout=6)
        response = client.responses.create(**kwargs)
        llm._apply_usage(result, response)
        data = json.loads(response.output_text)
        result.ok = True
        result.text = ""
        return data
    except Exception as e:
        result.error = str(e) or type(e).__name__
        raise
    finally:
        result.total = time.monotonic() - started
        llm.log_call(result)


def _valid(data):
    return (isinstance(data, dict)
            and isinstance(data.get("question"), str)
            and data["question"].strip()
            and isinstance(data.get("changed"), bool)
            and isinstance(data.get("material"), bool)
            and isinstance(data.get("corrections"), list)
            and all(isinstance(c, str) for c in data["corrections"]))


def reconcile(pairs, context_lines, call):
    with_b = [p for p in pairs if p.get("b")]
    if not with_b:
        return None
    if all(equivalent(p["a_plain"], p["b"]) for p in with_b):
        return {"question": None, "changed": False, "material": False,
                "corrections": [], "skipped": True}
    try:
        data = call(arbiter_input(pairs, context_lines))
    except Exception:
        return None
    if not _valid(data):
        return None
    out = dict(data)
    out["skipped"] = False
    return out
