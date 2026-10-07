"""Lógica pura del copiloto (sin Qt): ventana de pregunta, colas dinámicas
de los prompts, cascada Luna→Sol y planificador de prewarm."""

import threading
import time

import prompts

_NO_QUESTION = "(transcript completo)"
_INTERVIEWER_PREFIX = "Entrevistador:"
_MINE_PREFIX = "Tú:"


def latest_interviewer(utterances):
    for line in reversed(utterances):
        if line.startswith(_MINE_PREFIX):
            continue
        if line.startswith(_INTERVIEWER_PREFIX):
            line = line[len(_INTERVIEWER_PREFIX):]
        text = line.strip()
        if text:
            return text
    return _NO_QUESTION


def question_window(utterances, max_interviewer=4, max_chars=1800):
    lines = list(utterances)
    if not lines:
        return []
    interviewer = [i for i, line in enumerate(lines)
                   if not line.startswith(_MINE_PREFIX)]
    if interviewer:
        start = interviewer[-max_interviewer:][0]
        window = lines[start:]
    else:
        window = lines[-6:]
    while len(window) > 1 and sum(len(line) for line in window) > max_chars:
        window = window[1:]
    return window


def _tail(mode_text, parts):
    return [{"role": "developer", "content": mode_text},
            {"role": "user", "content": "\n\n".join(parts)}]


def _common_parts(window, pinned):
    parts = []
    if pinned:
        parts.append(prompts.PINNED_HEADER + "\n"
                     + "\n".join("- " + p for p in pinned))
    return parts


def _window_part(window):
    return prompts.WINDOW_HEADER + "\n" + "\n".join(window)


def fast_tail(config, window, pinned, second_ear=None):
    parts = _common_parts(window, pinned)
    parts.append(_window_part(window))
    if second_ear:
        parts.append(prompts.SECOND_EAR_HEADER + "\n"
                     + "\n".join(second_ear))
    return _tail(config["fast_prompt"], parts)


def detail_tail(config, window, pinned, fast_model, fast_text, fast_ok,
                verified=None):
    has_fast = bool(fast_ok and fast_text and fast_text.strip())
    mode = (config["detail_prompt"] if has_fast
            else config["detail_alone_prompt"])
    parts = _common_parts(window, pinned)
    parts.append(_window_part(window))
    if verified and verified.get("question"):
        text = prompts.VERIFIED_HEADER + "\n" + verified["question"]
        if verified.get("material") and has_fast:
            text += "\n" + prompts.VERIFIED_MATERIAL_NOTE
        parts.append(text)
    if has_fast:
        parts.append(prompts.FAST_ANSWER_HEADER.format(model=fast_model)
                     + "\n<<<\n" + fast_text.strip() + "\n>>>")
    else:
        parts.append(prompts.FAST_ANSWER_FAILED)
    return _tail(mode, parts)


def deeper_tail(config, question, mine, previous, pinned):
    parts = _common_parts(None, pinned)
    parts.append("Intervención del entrevistador a la que hay que "
                 "responder:\n" + question)
    parts.append(prompts.MINE_HEADER + "\n" + (mine or "(nada todavía)"))
    for model, text, followup in previous:
        header = (prompts.PREVIOUS_FOLLOWUP_HEADER if followup
                  else prompts.PREVIOUS_ANSWER_HEADER)
        parts.append(header.format(model=model) + "\n<<<\n" + text
                     + "\n>>>")
    return _tail(config["smart_prompt"], parts)


def diagram_tail(config, window, pinned):
    parts = _common_parts(window, pinned)
    parts.append(_window_part(window))
    return _tail(config["diagram_prompt"], parts)


def merge_keyterms(user_terms, preset, max_terms=100, max_tokens=450):
    seen = set()
    merged = []
    tokens = 0
    for term in list(user_terms or []) + list(preset or []):
        term = (term or "").strip()
        key = term.lower()
        if not term or key in seen:
            continue
        cost = 2 * len(term.split())
        if len(merged) >= max_terms or tokens + cost > max_tokens:
            continue
        seen.add(key)
        merged.append(term)
        tokens += cost
    return merged


class Cascade:
    def __init__(self, fast_key, *, verify_required=False,
                 verify_deadline_s=3.5, clock=time.monotonic):
        self.fast_key = fast_key
        self.verify_required = verify_required
        self.verify_deadline_s = verify_deadline_s
        self._clock = clock
        self._created = clock()
        self._lock = threading.Lock()
        self.fast_done = False
        self.fast_ok = False
        self.fast_text = ""
        self.verify_done = not verify_required
        self.verified = None
        self.started = False
        self.cancelled = False
        self.thread = None
        self.snapshot = {}

    def fast_finished(self, ok, text):
        with self._lock:
            self.fast_done = True
            self.fast_ok = bool(ok)
            self.fast_text = text or ""

    def verify_finished(self, result):
        with self._lock:
            self.verified = result
            self.verify_done = True

    def cancel(self):
        with self._lock:
            self.cancelled = True

    def verify_expired(self, now=None):
        now = self._clock() if now is None else now
        return now - self._created >= self.verify_deadline_s

    def take_detail_start(self, now=None):
        with self._lock:
            if self.cancelled or self.started or not self.fast_done:
                return False
            if not (self.verify_done or self.verify_expired(now)):
                return False
            self.started = True
            return True


class _PrewarmState:
    def __init__(self):
        self.in_flight = False
        self.last_signature = None
        self.last_sent = None
        self.failures = 0
        self.disabled = False


class PrewarmScheduler:
    def __init__(self, debounce_s=1.2, min_interval_s=8.0, min_tokens=1100,
                 clock=time.monotonic):
        self.debounce_s = debounce_s
        self.min_interval_s = min_interval_s
        self.min_tokens = min_tokens
        self._clock = clock
        self._lock = threading.Lock()
        self._last_change = None
        self._states = {}

    def _state(self, name):
        return self._states.setdefault(name, _PrewarmState())

    def note_change(self):
        with self._lock:
            self._last_change = self._clock()

    def due(self, name, signature, est_tokens):
        with self._lock:
            state = self._state(name)
            now = self._clock()
            if state.disabled or state.in_flight:
                return False
            if est_tokens < self.min_tokens:
                return False
            if signature == state.last_signature:
                return False
            if (self._last_change is not None
                    and now - self._last_change < self.debounce_s):
                return False
            if (state.last_sent is not None
                    and now - state.last_sent < self.min_interval_s):
                return False
            return True

    def mark_sent(self, name, signature):
        with self._lock:
            state = self._state(name)
            state.in_flight = True
            state.last_signature = signature
            state.last_sent = self._clock()

    def mark_done(self, name, ok):
        with self._lock:
            state = self._state(name)
            state.in_flight = False
            if ok:
                state.failures = 0
            else:
                state.failures += 1
                if state.failures >= 2:
                    state.disabled = True

    def disabled(self, name):
        with self._lock:
            return self._state(name).disabled

    def reset(self):
        with self._lock:
            self._last_change = None
            self._states = {}
