"""Backend simulado (AUDIO_GPT_MOCK=1): imita el streaming de gpt-6-luna y
gpt-6.1-sol sin gastar tokens, para probar la app y el panel Ctrl+I.

Tiempos tomados de medidas públicas (Artificial Analysis, 2026-10):
gpt-6-luna sin razonamiento ≈ 0,75 s al primer token y ≈ 120 tok/s;
gpt-6.1-sol ≈ 55 tok/s, con varios segundos de razonamiento y búsqueda
web antes del primer token. El SDK de OpenAI emite deltas de pocos tokens
cada pocas decenas de ms; aquí se reproduce esa cadencia.

Modela también la caché de prompt como la documenta OpenAI: cada petición
escribe sus breakpoints explícitos y la siguiente reutiliza el prefijo
cacheado MÁS LARGO que coincida (prewarm y llamadas reales por igual).

Perfiles (AUDIO_GPT_MOCK=<perfil>):
  1 | fast   tiempos nominales.
  slow       Luna degradada a tier estándar (TTFT 2,5 s), Sol 18 s.
  flaky      errores a mitad de stream, Sol caído, prewarm fallando, vacíos.
  burst      Luna en deltas de 1 carácter cada 8 ms; Sol en un solo delta.
AUDIO_GPT_MOCK_SPEED=<factor> acelera (>1) o frena (<1) todos los tiempos.
AUDIO_GPT_MOCK_SCRIPT=<ruta.json> guion de transcripción propio:
[[segundos_de_espera, "Entrevistador"|"Tú", "texto"], ...].
"""

import hashlib
import os
import random
import threading
import time

import llm
import mock_answers

PROFILE = (os.environ.get("AUDIO_GPT_MOCK") or "fast").lower()
if PROFILE in ("1", "true", "yes", "on"):
    PROFILE = "fast"
SPEED = float(os.environ.get("AUDIO_GPT_MOCK_SPEED") or 1.0)

TIMING = {
    # (ttft_s, intervalo_s entre deltas, caracteres por delta)
    "fast": {"luna": (0.75, 0.04, 19), "sol": (6.0, 0.05, 11)},
    "slow": {"luna": (2.5, 0.06, 12), "sol": (18.0, 0.07, 9)},
    "flaky": {"luna": (0.9, 0.04, 19), "sol": (7.0, 0.05, 11)},
    "burst": {"luna": (0.75, 0.008, 1), "sol": (6.0, 0.05, 100000)},
}
PREWARM_S = {"luna": 0.6, "sol": 1.3}
ARBITER_S = 0.9

_LOCK = threading.Lock()
_CACHE = {}            # modelo -> {hash_prefijo: tokens}
_CALLS = []            # registro para tests: dicts con kind/model/kwargs
_COUNTERS = {}


def enabled():
    return bool(os.environ.get("AUDIO_GPT_MOCK"))


def reset():
    with _LOCK:
        _CACHE.clear()
        _CALLS.clear()
        _COUNTERS.clear()


def calls(kind=None):
    with _LOCK:
        return [c for c in _CALLS if kind is None or c["kind"] == kind]


def _count(name):
    with _LOCK:
        _COUNTERS[name] = _COUNTERS.get(name, 0) + 1
        return _COUNTERS[name]


def _family(model):
    return "sol" if "sol" in (model or "").lower() else "luna"


def _sleep(seconds, cancel=None):
    """Duerme en pasos cortos comprobando cancel; True si se canceló."""
    end = time.monotonic() + seconds / SPEED
    while True:
        if cancel is not None and cancel.is_set():
            return True
        remaining = end - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.02, remaining))


# ------------------------- caché de prompt simulada -------------------------

def _block_text(content):
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content)


def _input_tokens(kwargs):
    return sum(llm.estimate_tokens(_block_text(i.get("content", "")))
               for i in kwargs.get("input", [])
               if i.get("type") != "configuration_update")


def _prefixes(kwargs):
    """[(hash_prefijo, tokens_prefijo)] en cada breakpoint explícito, en
    orden de aparición (prefijo cada vez más largo)."""
    hasher = hashlib.sha1(kwargs.get("model", "").encode())
    tokens = 0
    out = []
    for item in kwargs.get("input", []):
        if item.get("type") == "configuration_update":
            continue
        content = item.get("content", "")
        blocks = content if isinstance(content, list) else [
            {"text": content}]
        for block in blocks:
            text = block.get("text", "")
            hasher.update(text.encode("utf-8", "replace"))
            tokens += llm.estimate_tokens(text)
            if block.get("prompt_cache_breakpoint"):
                out.append((hasher.hexdigest(), tokens))
    return out


def _cache_lookup_and_write(kwargs):
    """Devuelve (input_tokens, cached_tokens, cache_write_tokens) y escribe
    los breakpoints de esta petición, como hace la API."""
    model = kwargs.get("model", "")
    total = _input_tokens(kwargs)
    prefixes = _prefixes(kwargs)
    with _LOCK:
        store = _CACHE.setdefault(model, {})
        cached = 0
        for digest, tokens in reversed(prefixes):      # el más largo primero
            if digest in store:
                cached = tokens
                break
        written = 0
        for digest, tokens in prefixes:
            if digest not in store:
                store[digest] = tokens
                written = max(written, tokens - cached)
    return total, cached, written


def cached_fraction(kwargs):
    """Solo lectura: qué parte del input encontraría ya en caché."""
    model = kwargs.get("model", "")
    with _LOCK:
        store = _CACHE.get(model, {})
        for digest, tokens in reversed(_prefixes(kwargs)):
            if digest in store:
                return tokens / max(1, _input_tokens(kwargs))
    return 0.0


# ------------------------- fallos del perfil flaky -------------------------

def _flaky_plan(kind):
    """Qué le pasa a esta llamada en el perfil flaky."""
    if PROFILE != "flaky":
        return None
    n = _count(kind)
    if kind == "fast":
        if n % 4 == 3:
            return ("mid_error", "rate_limit_exceeded: Fast mode capacity")
        if n % 5 == 0:
            return ("empty", "")
    elif kind == "detail":
        if n % 2 == 0:
            return ("early_error", "server_error: upstream timeout")
    elif kind == "deeper":
        if n % 3 == 0:
            return ("mid_error", "connection reset")
    elif kind == "prewarm":
        if n % 2 == 0:
            return ("early_error", "429 rate limited")
    return None


# ------------------------- stream -------------------------

def _record(kind, kwargs, result):
    with _LOCK:
        _CALLS.append({"kind": kind, "model": kwargs.get("model", ""),
                       "kwargs": kwargs, "result": result,
                       "t": time.monotonic()})


def stream(client, kwargs, *, on_delta=None, on_status=None, cancel=None,
           kind=""):
    started = time.monotonic()
    result = llm.CallResult(model=kwargs.get("model", ""), kind=kind)
    family = _family(result.model)
    ttft, interval, chunk = TIMING.get(PROFILE, TIMING["fast"])[family]
    # Con caché el prefill casi desaparece; sin ella se paga el transcript.
    cache_hit = cached_fraction(kwargs)
    ttft_eff = ttft * (0.7 + 0.3 * (1 - cache_hit))
    if kind == "deeper":
        ttft_eff *= 1.5
    text = mock_answers.answer_for(kind, kwargs)
    plan = _flaky_plan(kind)
    if plan and plan[0] == "empty":
        text = ""
    searching = (kwargs.get("tools") and kwargs.get("tool_choice") != "none"
                 and family == "sol")

    def finish(out_text, error="", status=""):
        result.text = out_text
        result.error = error
        result.status = status or ("completed" if not error else "failed")
        if not error and not out_text.strip():
            result.error, result.status = "sin texto", "empty"
        result.ok = not result.error and bool(out_text.strip())
        result.output_tokens = llm.estimate_tokens(out_text)
        result.total = time.monotonic() - started
        _record(kind, kwargs, result)
        llm.log_call(result)
        return result

    if plan and plan[0] == "early_error":
        _sleep(min(1.0, ttft_eff * 0.4), cancel)
        return finish("", plan[1])

    if searching:
        for fraction, status in ((0.25, "buscando en la web…"),
                                 (0.45, "leyendo resultados…"),
                                 (0.30, None)):
            if _sleep(ttft_eff * fraction, cancel):
                return finish("", "cancelada", "cancelled")
            if status and on_status:
                on_status(status)
    elif _sleep(ttft_eff, cancel):
        return finish("", "cancelada", "cancelled")

    (result.input_tokens, result.cached_tokens,
     result.cache_write_tokens) = _cache_lookup_and_write(kwargs)
    parts = []
    pos = 0
    rng = random.Random(len(text))
    fail_at = (int(len(text) * 0.4)
               if plan and plan[0] == "mid_error" else None)
    while pos < len(text):
        size = 1
        if chunk > 1:
            size = max(1, int(rng.gauss(chunk, chunk * 0.35)))
        piece = text[pos:pos + size]
        pos += size
        if result.ttft is None:
            result.ttft = time.monotonic() - started
        parts.append(piece)
        if on_delta:
            on_delta("".join(parts))
        if fail_at is not None and pos >= fail_at:
            return finish("".join(parts), plan[1])
        if pos < len(text) and _sleep(interval, cancel):
            return finish("".join(parts), "cancelada", "cancelled")
    return finish(text)


def prewarm(api_key, profile, utterances):
    started = time.monotonic()
    result = llm.CallResult(model=profile.model, kind="prewarm")
    kwargs = llm.request_kwargs(profile, utterances, prewarm=True)
    plan = _flaky_plan("prewarm")
    _sleep(PREWARM_S[_family(profile.model)])
    if plan:
        result.error = plan[1]
        result.status = "failed"
    else:
        (result.input_tokens, result.cached_tokens,
         result.cache_write_tokens) = _cache_lookup_and_write(kwargs)
        result.ok = True
        result.status = "completed"
    result.total = time.monotonic() - started
    _record("prewarm", kwargs, result)
    llm.log_call(result)
    return result


def ping(api_key, model):
    _sleep(0.05)
    _record("ping", {"model": model}, None)


def get_client(api_key):
    return object()


def call_arbiter(api_key, config, text):
    _sleep(ARBITER_S)
    data = mock_answers.arbiter_for(text)
    _record("arbiter", {"model": config.get("arbiter_model", "")}, data)
    return data


# ------------------------- instalación en la app -------------------------

def install(feed=True):
    """Sustituye las llamadas de red por el simulador. main.py lo llama
    antes de crear la ventana cuando AUDIO_GPT_MOCK está definido."""
    import verify
    from api_client import ApiKeyManager
    llm.stream = stream
    llm.prewarm = prewarm
    llm.ping = ping
    llm.get_client = get_client
    verify.call_arbiter = call_arbiter
    ApiKeyManager.load_api_key = staticmethod(
        lambda provider="openai": "mock-key")
    if feed:
        import mock_feed
        mock_feed.install()
