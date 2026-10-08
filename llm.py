"""Peticiones OpenAI (Responses API) con prompt cache explícito.

Prefijo estable (developer: system + brief + formato) + transcript en bloques
con breakpoints sobre los últimos N + cola dinámica. El prewarm envía el mismo
prefijo sin generar nada, de modo que la siguiente petición real lo reutiliza.
"""

import json
import os
import threading
import time
from dataclasses import dataclass

import prompts
from api_client import brief_block

BREAKPOINT_WINDOW = 32
_BREAKPOINT = {"mode": "explicit"}
_CACHE_TERMS = ("prompt_cache", "breakpoint", "configuration_update",
                "verbosity")
_PARAM_TERMS = ("service_tier", "reasoning", "unsupported", "unknown",
                "invalid")

_LOG_LOCK = threading.Lock()
_CLIENTS = {}
_CLIENTS_LOCK = threading.Lock()


@dataclass(frozen=True)
class Profile:
    name: str
    model: str
    effort: str
    service_tier: str | None
    verbosity: str | None
    tools: tuple = ()
    max_output_tokens: int = 2000
    stable: str = ""
    prewarm_service_tier: str | None = "auto"


def stable_text(config, brief):
    parts = (config.get("system_prompt", ""), brief_block(brief),
             config.get("format_prompt", ""))
    return "\n\n".join(p for p in parts if p)


def luna_profile(config, brief):
    return Profile(
        name="luna", model=config["model"],
        effort=config.get("reasoning_effort") or "none",
        service_tier=config.get("service_tier"),
        verbosity=config.get("fast_verbosity"),
        max_output_tokens=config.get("max_tokens", 2000),
        stable=stable_text(config, brief),
        prewarm_service_tier=config.get("prewarm_service_tier", "auto"))


def sol_profile(config, brief):
    tools = (({"type": "web_search"},)
             if config.get("smart_web_search") else ())
    return Profile(
        name="sol", model=config["detail_model"],
        effort=config["detail_reasoning_effort"],
        service_tier=config.get("smart_service_tier"),
        verbosity=config.get("detail_verbosity"), tools=tools,
        max_output_tokens=config.get("detail_max_tokens", 6000),
        stable=stable_text(config, brief),
        prewarm_service_tier=config.get("prewarm_service_tier", "auto"))


def build_input(stable, utterances, tail_items=(),
                breakpoint_window=BREAKPOINT_WINDOW):
    items = [{"role": "developer", "content": [{
        "type": "input_text", "text": stable,
        "prompt_cache_breakpoint": dict(_BREAKPOINT)}]}]
    if utterances:
        blocks = [{"type": "input_text", "text": prompts.TRANSCRIPT_HEADER}]
        first_marked = len(utterances) - breakpoint_window
        for i, line in enumerate(utterances):
            block = {"type": "input_text", "text": line + "\n"}
            if i >= first_marked:
                block["prompt_cache_breakpoint"] = dict(_BREAKPOINT)
            blocks.append(block)
        items.append({"role": "user", "content": blocks})
    items.extend(tail_items)
    return items


def request_kwargs(profile, utterances, tail_items=(), *, prewarm=False,
                   tool_choice=None, effort_override=None,
                   max_output_tokens=None):
    items = build_input(profile.stable, utterances,
                        () if prewarm else tail_items)
    if (effort_override and effort_override != profile.effort
            and not prewarm):
        items.append({"type": "configuration_update",
                      "reasoning": {"effort": effort_override}})
    cache = {"mode": "explicit"}
    if prewarm:
        cache["prewarm"] = True
    kwargs = {
        "model": profile.model,
        "input": items,
        "reasoning": {"effort": profile.effort},
        "prompt_cache_options": cache,
    }
    tier = profile.prewarm_service_tier if prewarm else profile.service_tier
    if tier and tier != "auto":
        kwargs["service_tier"] = tier
    if profile.verbosity:
        kwargs["text"] = {"verbosity": profile.verbosity}
    if profile.tools:
        kwargs["tools"] = list(profile.tools)
        choice = tool_choice or ("none" if prewarm else None)
        if choice:
            kwargs["tool_choice"] = choice
    if not prewarm:
        kwargs["max_output_tokens"] = (
            max_output_tokens or profile.max_output_tokens)
    return kwargs


def _item_text(content):
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content)


def plain_kwargs(kwargs):
    items = kwargs["input"]
    instructions = _item_text(items[0]["content"])
    texts = []
    for item in items[1:]:
        if item.get("type") == "configuration_update":
            continue
        text = _item_text(item["content"]).rstrip("\n")
        if text:
            texts.append(text)
    effort = kwargs.get("reasoning", {}).get("effort")
    for item in items:
        if item.get("type") == "configuration_update":
            effort = item["reasoning"]["effort"]
    out = {"model": kwargs["model"], "instructions": instructions,
           "input": "\n\n".join(texts)}
    if effort:
        out["reasoning"] = {"effort": effort}
    for key in ("service_tier", "tools", "tool_choice", "max_output_tokens"):
        if key in kwargs:
            out[key] = kwargs[key]
    return out


def estimate_tokens(text):
    return int(len(text) / 3.2)


@dataclass
class CallResult:
    ok: bool = False
    text: str = ""
    error: str = ""
    status: str = ""
    model: str = ""
    kind: str = ""
    ttft: float | None = None
    total: float | None = None
    input_tokens: int | None = None
    cached_tokens: int | None = None
    cache_write_tokens: int | None = None
    output_tokens: int | None = None

    def metrics_label(self):
        parts = []
        if self.total is not None:
            parts.append(f"{self.total:.1f}s")
        if self.ttft is not None:
            parts.append(f"1er token {self.ttft:.1f}s")
        if self.input_tokens and self.cached_tokens is not None:
            parts.append(
                f"caché {round(100 * self.cached_tokens / self.input_tokens)}%")
        return " · ".join(parts)


def _http_client():
    import openai
    try:
        import httpx2 as httpx
    except ImportError:
        import httpx
    return openai.DefaultHttpxClient(limits=httpx.Limits(
        max_connections=20, max_keepalive_connections=10,
        keepalive_expiry=120))


def get_client(api_key):
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(api_key)
        if client is None:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, http_client=_http_client())
            _CLIENTS[api_key] = client
        return client


def _g(obj, name, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _apply_usage(result, response):
    result.status = _g(response, "status", "") or result.status
    usage = _g(response, "usage")
    if usage is None:
        return
    details = _g(usage, "input_tokens_details")
    result.input_tokens = _g(usage, "input_tokens")
    result.output_tokens = _g(usage, "output_tokens")
    result.cached_tokens = _g(details, "cached_tokens")
    result.cache_write_tokens = _g(details, "cache_write_tokens")


def _error_text(err):
    if err is None:
        return "error desconocido"
    if isinstance(err, str):
        return err
    return str(_g(err, "message") or err)


def _retry_kwargs(kwargs, message):
    message = message.lower()
    if any(t in message for t in _CACHE_TERMS):
        return plain_kwargs(kwargs)
    if any(t in message for t in _PARAM_TERMS):
        plain = plain_kwargs(kwargs)
        plain.pop("service_tier", None)
        plain.pop("reasoning", None)
        return plain
    return None


def log_call(result):
    if os.environ.get("AUDIO_GPT_NO_LOG"):
        return
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "kind": result.kind, "model": result.model, "ok": result.ok,
        "status": result.status, "ttft": result.ttft, "total": result.total,
        "input_tokens": result.input_tokens,
        "cached_tokens": result.cached_tokens,
        "cache_write_tokens": result.cache_write_tokens,
        "output_tokens": result.output_tokens,
        "error": (result.error or "")[:200],
    }
    try:
        folder = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "logs")
        with _LOG_LOCK:
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, "llm_calls.jsonl"), "a",
                      encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _close(stream):
    try:
        stream.close()
    except Exception:
        pass


def _run_stream(client, kwargs, result, started, parts, on_delta,
                on_status, cancel):
    stream = client.responses.create(stream=True, **kwargs)
    try:
        for event in stream:
            if cancel is not None and cancel.is_set():
                result.error = "cancelada"
                result.status = "cancelled"
                return
            etype = _g(event, "type", "")
            if etype == "response.output_text.delta":
                delta = _g(event, "delta", "")
                if delta:
                    if result.ttft is None:
                        result.ttft = time.monotonic() - started
                    parts.append(delta)
                    if on_delta:
                        on_delta("".join(parts))
            elif etype in ("response.web_search_call.in_progress",
                           "response.web_search_call.searching"):
                if on_status:
                    on_status("buscando en la web…")
            elif etype == "response.web_search_call.completed":
                if on_status:
                    on_status("leyendo resultados…")
            elif etype in ("response.completed", "response.incomplete",
                           "response.failed"):
                response = _g(event, "response")
                _apply_usage(result, response)
                if etype == "response.failed":
                    result.error = _error_text(_g(response, "error"))
                break
            elif etype in ("error", "response.error"):
                result.error = _error_text(_g(event, "error", event))
                break
    finally:
        _close(stream)


def stream(client, kwargs, *, on_delta=None, on_status=None, cancel=None,
           kind=""):
    from openai import BadRequestError
    started = time.monotonic()
    result = CallResult(model=kwargs.get("model", ""), kind=kind)
    parts = []
    retried = False
    while True:
        parts = []
        result.error = ""
        try:
            _run_stream(client, kwargs, result, started, parts, on_delta,
                        on_status, cancel)
        except BadRequestError as e:
            fallback = None if retried or result.ttft is not None else \
                _retry_kwargs(kwargs, str(e))
            if fallback is not None:
                retried = True
                kwargs = fallback
                continue
            result.error = str(e)
        except Exception as e:
            result.error = str(e) or type(e).__name__
        break
    result.text = "".join(parts)
    result.total = time.monotonic() - started
    if not result.error and not result.text.strip():
        result.error = "sin texto"
        result.status = result.status or "empty"
    result.ok = not result.error and bool(result.text.strip())
    log_call(result)
    return result


def prewarm(api_key, profile, utterances):
    started = time.monotonic()
    result = CallResult(model=profile.model, kind="prewarm")
    try:
        kwargs = request_kwargs(profile, utterances, prewarm=True)
        response = get_client(api_key).responses.create(**kwargs)
        _apply_usage(result, response)
        result.ok = (result.status or "completed") == "completed"
        if not result.ok:
            result.error = f"status {result.status}"
    except Exception as e:
        result.error = str(e) or type(e).__name__
    result.total = time.monotonic() - started
    log_call(result)
    return result


def ping(api_key, model):
    try:
        get_client(api_key).models.retrieve(model)
    except Exception:
        pass
