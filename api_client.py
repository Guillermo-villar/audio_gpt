"""Clientes de API: transcripción (vía transcriber.py) y GPT.

Cambios respecto a la versión original:
- whisper-1 -> gpt-4o-transcribe (y más proveedores: groq, local, realtime).
- chat.completions + gpt-3.5-turbo -> Responses API con gpt-6-luna.
- La API key se puede dar por variable de entorno además de api_key.txt.
"""

import os
import json
import re
import shutil
import subprocess
import tempfile
import urllib.request

from dotenv import load_dotenv
from PySide6.QtCore import QThread, Signal

import prompts
import transcriber

load_dotenv()


def _key_file(provider):
    return "api_key.txt" if provider in ("openai", "openai-realtime") else f"{provider}_api_key.txt"


def cloudflare_creds():
    """(token, account_id) para Workers AI — env primero, luego archivos
    cloudflare_api_key.txt / cloudflare_account_id.txt (gitignored)."""
    token = (os.environ.get("CLOUDFLARE_API_TOKEN")
             or ApiKeyManager.load_api_key("cloudflare"))
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    if not account:
        try:
            with open("cloudflare_account_id.txt", "r") as f:
                account = f.read().strip()
        except OSError:
            pass
    return token or "", account


# Palabras que delatan una pregunta/encargo en ES/EN. Puerta barata: si no
# aparece ninguna y no hay «?», casi seguro es charla y no vale llamar al LLM.
_QUESTION_MARKERS = (
    "what", "how", "why", "when", "where", "who", "which", "explain",
    "describe", "implement", "write", "design", "optimize", "walk me",
    "tell me", "difference between", "would you", "could you", "given",
    "qué", "cómo", "cuándo", "dónde", "por qué", "quién", "cuál",
    "cuáles", "explica", "describe", "implementa", "dime", "cuéntame",
    "diseña", "optimiza", "escribe", "podrías", "diferencia entre",
)
_TECH_TASK_HINTS = (
    "algorithm", "algoritmo", "leetcode", "complexity", "complejidad",
    "big-o", "big o", "system design", "sql", "query", "array", "tree",
    "linked list", "hashmap", "binary search", "deadlock", "race condition",
)


_MARKER_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _QUESTION_MARKERS) + r")\b")


def looks_like_question(text, min_words=4):
    """Puerta local: ¿esto suena a pregunta/encargo técnico? Heurística,
    sin LLM — sólo evita quemar llamadas en muletillas y charla.
    «?» explícito pasa sin mínimo de palabras (¿y la complejidad?, Why?);
    los marcadores usan límites de palabra («whatever» no dispara «what»)."""
    t = text.strip().lower()
    words = t.split()
    if not words:
        return False
    if "?" in t or "¿" in t:
        return True                          # «Why?», «¿Cómo?» explícitas
    if len(words) < min_words:
        return False
    if _MARKER_RE.search(t):
        return True
    return any(h in t for h in _TECH_TASK_HINTS)


_WEB_SEARCH_HINTS = (
    "latest", "current", "today", "news", "recent", "release", "version",
    "price", "pricing", "stock", "who won", "documentation", "docs",
    "look up", "search", "google", "web", "internet",
    "última", "último", "actual", "actualidad", "hoy", "noticias",
    "reciente", "versión", "precio", "cuánto cuesta", "quién ganó",
    "documentación", "busca", "buscar",
)


def exa_api_key():
    """EXA_API_KEY o exa_api_key.txt (cubierto por *_api_key.txt)."""
    if os.environ.get("EXA_API_KEY"):
        return os.environ["EXA_API_KEY"].strip()
    try:
        with open("exa_api_key.txt", "r") as f:
            return f.read().strip()
    except OSError:
        return ""


_WEB_SEARCH_RE = re.compile(
    r"(?<!\w)(?:"
    + "|".join(re.escape(h) for h in sorted(_WEB_SEARCH_HINTS, key=len,
                                            reverse=True))
    + r")(?!\w)|\b20\d{2}\b",
    re.IGNORECASE)


def needs_web_search(text):
    """Gate barato: solo busca si la pregunta pide datos externos/actuales
    (pistas como palabra o frase completa, no como subcadena)."""
    return bool(_WEB_SEARCH_RE.search(text or ""))


def exa_search(query, max_results=3, timeout=4):
    """Búsqueda Exa rápida: título, URL y highlights cortos para el LLM."""
    key = exa_api_key()
    if not key:
        return ""
    body = {
        "query": query[:1000],
        "type": "fast",
        "numResults": max(1, min(int(max_results), 5)),
        "contents": {"highlights": True},
    }
    try:
        req = urllib.request.Request(
            "https://api.exa.ai/search",
            data=json.dumps(body).encode(),
            method="POST",
            headers={"x-api-key": key, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            results = json.loads(r.read()).get("results", [])
    except Exception:
        return ""

    lines = []
    for item in results:
        title = (item.get("title") or item.get("url") or "Fuente").strip()
        url = (item.get("url") or "").strip()
        snippets = item.get("highlights") or []
        if isinstance(snippets, str):
            snippets = [snippets]
        snippet = " ".join(s.strip() for s in snippets if s.strip())[:900]
        lines.append(f"- {title}\n  {url}\n  {snippet}".rstrip())
    return "\n".join(lines)


class ApiKeyManager:
    """Gestiona el almacenamiento y recuperación de API keys por proveedor."""

    @staticmethod
    def save_api_key(api_key, provider="openai"):
        """Guarda la API key en un archivo local (ignorado por git)."""
        try:
            with open(_key_file(provider), "w") as f:
                f.write(api_key)
            return True
        except Exception as e:
            print(f"Error al guardar API key: {e}")
            return False

    @staticmethod
    def load_api_key(provider="openai"):
        """Orden de búsqueda: variable de entorno -> archivo local."""
        env_name = transcriber.PROVIDERS.get(provider, {}).get("key_env")
        if env_name and os.environ.get(env_name):
            return os.environ[env_name].strip()
        try:
            path = _key_file(provider)
            if os.path.exists(path):
                with open(path, "r") as f:
                    return f.read().strip()
        except Exception as e:
            print(f"Error al cargar API key: {e}")
        return ""


class TranscriptionThread(QThread):
    """Hilo para transcribir un archivo de audio sin bloquear la UI."""

    transcription_complete = Signal(bool, str)

    def __init__(self, api_key, filename, language=None,
                 provider=transcriber.DEFAULT_PROVIDER, model=transcriber.DEFAULT_MODEL):
        super().__init__()
        self.api_key = api_key
        self.filename = filename
        self.language = language
        self.provider = provider
        self.model = model

    def run(self):
        try:
            text = transcriber.transcribe_file(
                self.api_key, self.filename, self.language,
                provider=self.provider, model=self.model,
            )
            self.transcription_complete.emit(True, text)
        except Exception as e:
            self.transcription_complete.emit(False, str(e))


class WhisperService:
    """Fachada de compatibilidad sobre transcriber.py."""

    @staticmethod
    def get_available_languages():
        return {
            "": "Auto-detectar",
            "es": "Español",
            "en": "Inglés",
            "fr": "Francés",
            "de": "Alemán",
            "it": "Italiano",
            "pt": "Portugués",
            "nl": "Holandés",
            "ru": "Ruso",
            "zh": "Chino",
            "ja": "Japonés",
            "ar": "Árabe",
        }

    @staticmethod
    def transcribe_file(api_key, file_path, language=None,
                        provider=transcriber.DEFAULT_PROVIDER,
                        model=transcriber.DEFAULT_MODEL):
        """Transcribe un archivo de forma síncrona (útil en scripts)."""
        try:
            return transcriber.transcribe_file(
                api_key, file_path, language, provider=provider, model=model
            )
        except Exception as e:
            return f"[Error: {str(e)}]"


DEFAULT_GPT_CONFIG = {
    "model": "gpt-6-luna",   # ~7x más barato que 5.4-mini; effort dial: low->hard
    "system_prompt": prompts.SYSTEM_PROMPT,
    "temperature": None,          # los modelos de razonamiento (gpt-5/6) no admiten temperature
    "reasoning_effort": "none",   # borradores rápidos; la revisión sube a "medium"
    "service_tier": "fast",       # Fast mode (~2.5x menos latencia, ~2x precio); "auto"/"flex" alternativas
    "max_tokens": 2000,
    "format_prompt": prompts.PANEL_FORMAT,
    "fast_prompt": prompts.FAST_MODE,
    "smart_prompt": prompts.DEEPER_MODE,
    "diagram_prompt": prompts.DIAGRAM_MODE,
    "diagram_reasoning_effort": "low",
    "diagram_max_tokens": 1500,
    "verify_enabled": True,
    "verify_stt_model": "gpt-transcribe",
    "arbiter_model": "gpt-6-luna",
    "arbiter_prompt": prompts.ARBITER_PROMPT,
    "verify_wait_s": 2.0,
    "verify_deadline_s": 3.5,
    "detail_prompt": prompts.DETAIL_MODE,
    "detail_alone_prompt": prompts.DETAIL_MODE_ALONE,
    "detail_enabled": True,
    "detail_model": "gpt-6.1-sol",
    "detail_reasoning_effort": "medium",
    "detail_max_tokens": 6000,
    "fast_verbosity": "low",
    "detail_verbosity": "medium",
    "prewarm": True,
    "sd_keyterms": True,
    "smart_model": "gpt-6.1-sol",
    "smart_reasoning_effort": "medium",
    "smart_service_tier": "fast",
    "smart_web_search": True,
    "smart_max_tokens": 6000,
    "cf_model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    "cf_fast_model": "@cf/meta/llama-3.1-8b-instruct-fast",
    "web_search": "auto",         # exa si hay EXA_API_KEY/exa_api_key.txt y la pregunta lo requiere
    "web_search_results": 3,
    "web_search_timeout": 4,
}


def brief_block(brief):
    return prompts.BRIEF_BLOCK.format(brief=brief) if brief else ""


def build_smart_request(question, previous, context, mine, brief, config):
    instructions = config.get("system_prompt", "Eres un asistente útil.")
    if brief:
        instructions += "\n\n" + brief_block(brief)
    format_prompt = config.get("format_prompt", "")
    if format_prompt:
        instructions += "\n\n" + format_prompt
    smart_prompt = config.get("smart_prompt", "")
    if smart_prompt:
        instructions += "\n\n" + smart_prompt

    previous_blocks = []
    for answer in previous:
        model, text, *followup = answer
        if followup and followup[0]:
            heading = f"Respuesta anterior de {model} (tampoco le sirvió; profundiza más):"
        else:
            heading = f"Respuesta anterior de {model} (no le sirvió al candidato):"
        previous_blocks.append(f"{heading}\n<<<\n{text}\n>>>")

    user_input = (
        "Transcript completo de la llamada (Entrevistador = voces de la llamada, "
        "Tú = el candidato; puede tener errores de transcripción):\n"
        f"{context}\n\n"
        "Intervención del entrevistador a la que hay que responder:\n"
        f"{question}\n\n"
        "Lo que el candidato (Tú) ha dicho desde esa intervención:\n"
        f"{mine or '(nada todavía)'}\n\n"
        + "\n\n".join(previous_blocks)
    )
    return instructions, user_input


class GptClient:
    """Cliente GPT usando la Responses API (la API recomendada actualmente)."""

    @staticmethod
    def load_config():
        """Carga gpt_config.json; si no existe lo crea con valores actuales."""
        config_path = os.path.join(os.path.dirname(__file__), "gpt_config.json")

        if not os.path.exists(config_path):
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_GPT_CONFIG, f, indent=2, ensure_ascii=False)
            return dict(DEFAULT_GPT_CONFIG)

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            # Rellenar claves nuevas que falten en configs antiguas
            merged = dict(DEFAULT_GPT_CONFIG)
            merged.update(cfg)
            return merged
        except Exception as e:
            print(f"Error al cargar la configuración GPT: {e}")
            return None

    @staticmethod
    def send_to_gpt(api_key, transcription, engine="openai", context="",
                    effort=None, max_tokens=None, brief="", on_delta=None,
                    allow_web=False, fast=False, instructions_override=None):
        """Envía la transcripción a GPT y devuelve (ok, respuesta_o_error).

        engine="cloudflare" usa Workers AI: modelos nativos @cf/* por chat
        completions y modelos compatibles por Responses; "openai" usa la API
        normal. `context` son las últimas intervenciones etiquetadas por
        carril. `effort` sube el razonamiento para la pasada de revisión.
        """
        try:
            from openai import OpenAI, BadRequestError
        except ImportError:
            return False, "El paquete openai no está instalado"

        config = GptClient.load_config()
        if not config:
            return False, "Error al cargar la configuración de GPT"

        if engine == "cloudflare":
            token, account = cloudflare_creds()
            if not token or not account:
                return False, (
                    "Cloudflare necesita CLOUDFLARE_API_TOKEN y "
                    "CLOUDFLARE_ACCOUNT_ID (o los archivos "
                    "cloudflare_api_key.txt / cloudflare_account_id.txt)"
                )
            client = OpenAI(
                api_key=token,
                base_url=(f"https://api.cloudflare.com/client/v4/accounts/"
                          f"{account}/ai/v1"),
            )
            # Workers AI nativo va en créditos/free tier; los modelos de
            # terceros (openai/*) requieren saldo en la gateway o BYOK.
            model = config.get(
                "cf_fast_model" if fast else "cf_model",
                "@cf/meta/llama-3.1-8b-instruct-fast" if fast
                else "@cf/meta/llama-3.3-70b-instruct-fp8-fast")
        else:
            client = OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))
            model = config.get("model", DEFAULT_GPT_CONFIG["model"])

        instructions = (
            instructions_override if instructions_override is not None
            else config.get("system_prompt", "Eres un asistente útil.")
        )
        if brief:
            # El brief es identidad/política (system), no contenido (input):
            # experiencia aprobada + límites explícitos contra fabricación.
            instructions += "\n\n" + brief_block(brief)
        format_prompt = config.get("format_prompt", "")
        if format_prompt and instructions_override is None:
            instructions += "\n\n" + format_prompt
            fast_prompt = config.get("fast_prompt", "")
            if fast_prompt:
                instructions += "\n\n" + fast_prompt

        web_context = ""
        if allow_web and config.get("web_search") in (True, "auto", "exa"):
            if needs_web_search(transcription):
                web_context = exa_search(
                    transcription,
                    max_results=config.get("web_search_results", 3),
                    timeout=config.get("web_search_timeout", 4),
                )

        if context or web_context:
            gpt_input = (
                f"Contexto de la conversación (transcript completo de la "
                f"llamada, solo como referencia):\n{context}\n\n"
                f"Última intervención del entrevistador — esto es lo que "
                f"hay que responder ahora: {transcription}")
            if web_context:
                gpt_input += (
                    "\n\nContexto web reciente (úsalo solo si es relevante; "
                    "cita la URL brevemente al final):\n" + web_context)
        else:
            gpt_input = f"Transcription: {transcription}"

        kwargs = {
            "model": model,
            "instructions": instructions,
            "input": gpt_input,
            "max_output_tokens": max_tokens or config.get("max_tokens", 2000),
        }
        if config.get("temperature") is not None:
            kwargs["temperature"] = config["temperature"]
        if config.get("top_p") is not None:
            kwargs["top_p"] = config["top_p"]
        effort = effort or config.get("reasoning_effort")
        if effort and effort != "none":
            kwargs["reasoning"] = {"effort": effort}
        if (engine != "cloudflare"
                and config.get("service_tier")
                and config["service_tier"] != "auto"):
            kwargs["service_tier"] = config["service_tier"]

        try:
            if engine == "cloudflare" and model.startswith("@cf/"):
                # Workers AI nativo: endpoint de chat completions (el de
                # responses no acepta modelos @cf/*).
                chat_kwargs = {
                    "model": model,
                    "messages": [
                        {"role": "system",
                         "content": kwargs["instructions"]},
                        {"role": "user", "content": kwargs["input"]},
                    ],
                    "max_tokens": kwargs["max_output_tokens"],
                }
                if on_delta:
                    try:
                        parts = []
                        stream = client.chat.completions.create(
                            **chat_kwargs, stream=True)
                        for chunk in stream:
                            choice = (getattr(chunk, "choices", None) or [None])[0]
                            delta = getattr(getattr(choice, "delta", None), "content", None)
                            if delta:
                                parts.append(delta)
                                on_delta("".join(parts))
                        text = "".join(parts)
                        if text:
                            return True, text
                    except Exception:
                        pass    # si CF no permite stream, cae a respuesta completa
                response = client.chat.completions.create(**chat_kwargs)
                text = response.choices[0].message.content
                return (True, text) if text else (False, "La API no devolvió texto")

            if on_delta:
                try:
                    parts = []
                    stream = client.responses.create(**kwargs, stream=True)
                    for event in stream:
                        etype = getattr(event, "type", "")
                        if etype == "response.output_text.delta":
                            delta = getattr(event, "delta", "")
                            if delta:
                                parts.append(delta)
                                on_delta("".join(parts))
                        elif etype in ("response.completed", "response.done"):
                            break
                        elif etype == "response.error":
                            raise RuntimeError(getattr(event, "error", event))
                    text = "".join(parts)
                    if text:
                        return True, text
                except BadRequestError:
                    pass
                except Exception:
                    pass
            response = client.responses.create(**kwargs)
        except BadRequestError as e:
            # Los modelos de razonamiento rechazan temperature/top_p; los no
            # razonadores pueden rechazar `reasoning`/`service_tier`. Reintentar sin ellos.
            msg = str(e).lower()
            if "unsupported" in msg or "unknown" in msg or "invalid" in msg:
                for key in ("temperature", "top_p", "reasoning", "service_tier"):
                    kwargs.pop(key, None)
                try:
                    response = client.responses.create(**kwargs)
                except Exception as e2:
                    return False, f"Error al comunicarse con GPT: {e2}"
            else:
                return False, f"Error al comunicarse con GPT: {e}"
        except Exception as e:
            return False, f"Error al comunicarse con GPT: {e}"

        text = getattr(response, "output_text", None)
        if not text:
            return False, "La API no devolvió texto"
        return True, text

    @staticmethod
    def send_smarter(api_key, question, previous, context, mine, brief,
                     on_delta=None, on_status=None, effort=None):
        try:
            from openai import OpenAI, BadRequestError
        except ImportError:
            return False, "El paquete openai no está instalado"

        config = GptClient.load_config()
        if not config:
            return False, "Error al cargar la configuración de GPT"

        instructions, user_input = build_smart_request(
            question, previous, context, mine, brief, config)
        kwargs = {
            "model": config.get("smart_model", "gpt-6.1-sol"),
            "instructions": instructions,
            "input": user_input,
            "reasoning": {
                "effort": effort or config.get("smart_reasoning_effort", "medium")
            },
            "max_output_tokens": config.get("smart_max_tokens", 6000),
        }
        service_tier = config.get("smart_service_tier", "auto")
        if service_tier and service_tier != "auto":
            kwargs["service_tier"] = service_tier
        if config.get("smart_web_search", True):
            kwargs["tools"] = [{"type": "web_search"}]

        client = OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))

        def stream_response(request_kwargs):
            parts = []
            stream = client.responses.create(**request_kwargs, stream=True)
            for event in stream:
                event_type = getattr(event, "type", "")
                if event_type == "response.output_text.delta":
                    delta = getattr(event, "delta", "")
                    if delta:
                        parts.append(delta)
                        if on_delta:
                            on_delta("".join(parts))
                elif event_type in (
                        "response.web_search_call.in_progress",
                        "response.web_search_call.searching"):
                    if on_status:
                        on_status("buscando en la web…")
                elif event_type == "response.web_search_call.completed":
                    if on_status:
                        on_status("leyendo resultados…")
                elif event_type in ("response.completed", "response.done"):
                    break
                elif event_type == "response.error":
                    raise RuntimeError(getattr(event, "error", event))
            return "".join(parts)

        def retry_without_unsupported(request_kwargs):
            request_kwargs.pop("service_tier", None)
            request_kwargs.pop("reasoning", None)
            if on_delta:
                text = stream_response(request_kwargs)
                return (True, text) if text else (
                    False, "La API no devolvió texto")
            response = client.responses.create(**request_kwargs)
            text = getattr(response, "output_text", None)
            return (True, text) if text else (False, "La API no devolvió texto")

        if on_delta:
            try:
                text = stream_response(kwargs)
                if text:
                    return True, text
            except BadRequestError as error:
                message = str(error).lower()
                if "tools" in kwargs and any(
                        term in message for term in
                        ("web_search", "tools", "unsupported")):
                    kwargs.pop("tools", None)
                    web_context = exa_search(question)
                    if web_context:
                        kwargs["input"] += (
                            "\n\nContexto web reciente (úsalo solo si es relevante; "
                            "cita la URL brevemente al final):\n" + web_context)
                        if on_status:
                            on_status("web vía Exa…")
                    try:
                        text = stream_response(kwargs)
                        if text:
                            return True, text
                    except BadRequestError as retry_error:
                        message = str(retry_error).lower()
                        if not any(term in message for term in
                                   ("unsupported", "unknown", "invalid")):
                            return False, (
                                f"Error al comunicarse con GPT: {retry_error}")
                        try:
                            return retry_without_unsupported(kwargs)
                        except Exception as final_error:
                            return False, (
                                f"Error al comunicarse con GPT: {final_error}")
                    except Exception as retry_error:
                        return False, (
                            f"Error al comunicarse con GPT: {retry_error}")
                elif any(term in message for term in
                         ("unsupported", "unknown", "invalid")):
                    try:
                        return retry_without_unsupported(kwargs)
                    except Exception as retry_error:
                        return False, (
                            f"Error al comunicarse con GPT: {retry_error}")
                else:
                    return False, f"Error al comunicarse con GPT: {error}"
            except Exception as error:
                return False, f"Error al comunicarse con GPT: {error}"

        try:
            response = client.responses.create(**kwargs)
        except BadRequestError as error:
            message = str(error).lower()
            if "tools" in kwargs and any(
                    term in message for term in
                    ("web_search", "tools", "unsupported")):
                kwargs.pop("tools", None)
                web_context = exa_search(question)
                if web_context:
                    kwargs["input"] += (
                        "\n\nContexto web reciente (úsalo solo si es relevante; "
                        "cita la URL brevemente al final):\n" + web_context)
                    if on_status:
                        on_status("web vía Exa…")
            if any(term in message for term in
                   ("unsupported", "unknown", "invalid")):
                kwargs.pop("service_tier", None)
                kwargs.pop("reasoning", None)
            try:
                response = client.responses.create(**kwargs)
            except Exception as retry_error:
                return False, f"Error al comunicarse con GPT: {retry_error}"
        except Exception as error:
            return False, f"Error al comunicarse con GPT: {error}"
        text = getattr(response, "output_text", None)
        return (True, text) if text else (False, "La API no devolvió texto")

    @staticmethod
    def send_smarter_cloudflare(question, previous, context, mine, brief,
                                on_delta=None):
        config = GptClient.load_config() or dict(DEFAULT_GPT_CONFIG)
        instructions, user_input = build_smart_request(
            question, previous, context, mine, brief, config)
        if config.get("smart_web_search", True):
            web_context = exa_search(question)
            if web_context:
                user_input += (
                    "\n\nContexto web reciente (úsalo solo si es relevante; "
                    "cita la URL brevemente al final):\n" + web_context)
        return GptClient.send_to_gpt(
            None, user_input, engine="cloudflare", allow_web=False,
            instructions_override=instructions, on_delta=on_delta)


class GptQueryThread(QThread):
    """Hilo para enviar consultas a GPT sin bloquear la interfaz."""

    query_complete = Signal(bool, str)
    query_delta = Signal(str)   # texto acumulado mientras llega por streaming

    def __init__(self, api_key, transcription, engine="openai", context="",
                 effort=None, max_tokens=None, brief="", allow_web=False,
                 fast=False):
        super().__init__()
        self.api_key = api_key
        self.transcription = transcription
        self.engine = engine
        self.context = context
        self.effort = effort
        self.max_tokens = max_tokens
        self.brief = brief
        self.allow_web = allow_web
        self.fast = fast

    def run(self):
        success, result = GptClient.send_to_gpt(
            self.api_key, self.transcription, engine=self.engine,
            context=self.context, effort=self.effort,
            max_tokens=self.max_tokens, brief=self.brief,
            on_delta=self.query_delta.emit, allow_web=self.allow_web,
            fast=self.fast)
        self.query_complete.emit(success, result)


class SmartQueryThread(QThread):
    query_delta = Signal(str)
    query_status = Signal(str)
    query_complete = Signal(bool, str)

    def __init__(self, api_key, question, previous, context, mine, brief,
                 effort=None, engine="openai"):
        super().__init__()
        self.api_key = api_key
        self.question = question
        self.previous = previous
        self.context = context
        self.mine = mine
        self.brief = brief
        self.effort = effort
        self.engine = engine

    def run(self):
        if self.engine == "cloudflare":
            success, result = GptClient.send_smarter_cloudflare(
                self.question, self.previous, self.context, self.mine,
                self.brief, on_delta=self.query_delta.emit)
        else:
            success, result = GptClient.send_smarter(
                self.api_key, self.question, self.previous, self.context,
                self.mine, self.brief, on_delta=self.query_delta.emit,
                on_status=self.query_status.emit, effort=self.effort)
        self.query_complete.emit(success, result)


# El modelo con más margen de uso en los planes ChatGPT (Plus: ~350-3000
# mensajes locales / 5 h, mucho más que Sol o Astra). gpt-5.4-mini también está
# disponible en Codex al ~30 % de cuota si se prefiere.
CODEX_MODEL = "gpt-6-luna"


def codex_available():
    """True si la CLI de Codex está instalada y en el PATH."""
    return shutil.which("codex") is not None


class CodexCliThread(QThread):
    """Consulta GPT a través de `codex exec` (CLI incluida en ChatGPT Plus/Pro).

    Requiere `npm i -g @openai/codex` y `codex login` con la cuenta ChatGPT.
    Las peticiones consumen la cuota de la suscripción, no créditos de API.
    """

    query_complete = Signal(bool, str)

    def __init__(self, transcription, model=CODEX_MODEL, context="",
                 brief="", full_prompt=None, search=False):
        super().__init__()
        self.transcription = transcription
        self.model = model
        self.context = context
        self.brief = brief
        self.full_prompt = full_prompt
        self.search = search

    def run(self):
        exe = shutil.which("codex")
        if not exe:
            self.query_complete.emit(
                False,
                "No se encontró la CLI de Codex. Instálala con "
                "«npm i -g @openai/codex» y entra con «codex login» usando tu "
                "cuenta de ChatGPT."
            )
            return

        if self.full_prompt is not None:
            prompt = self.full_prompt
        else:
            config = GptClient.load_config() or dict(DEFAULT_GPT_CONFIG)
            prompt = config.get("system_prompt", "Eres un asistente útil.")
            if self.brief:
                prompt += "\n\n" + brief_block(self.brief)
            if config.get("format_prompt"):
                prompt += "\n\n" + config["format_prompt"]
                if config.get("fast_prompt"):
                    prompt += "\n\n" + config["fast_prompt"]
            if self.context:
                prompt += f"\n\nContexto de la conversación:\n{self.context}"
            prompt += "\n\nTranscription: " + self.transcription

        out_path = None
        try:
            with tempfile.NamedTemporaryFile(
                    suffix=".txt", delete=False) as tf:
                out_path = tf.name
            args = [
                "exec", "--skip-git-repo-check",
                "--sandbox", "read-only",
                "--output-last-message", out_path,
                "-m", self.model, prompt,
            ]
            proc = subprocess.run(
                [exe] + (["--search"] if self.search else []) + args,
                capture_output=True, text=True, timeout=300,
                encoding="utf-8", errors="replace")
            error_text = (proc.stderr or "").lower()
            if (self.search and proc.returncode != 0
                    and ("unexpected argument" in error_text
                         or "unrecognized" in error_text)):
                proc = subprocess.run(
                    [exe] + args, capture_output=True, text=True,
                    timeout=300, encoding="utf-8", errors="replace")
        except Exception as e:
            self.query_complete.emit(False, f"Codex CLI: {e}")
            return

        answer = ""
        if out_path and os.path.exists(out_path):
            try:
                with open(out_path, "r", encoding="utf-8") as f:
                    answer = f.read().strip()
            finally:
                try:
                    os.unlink(out_path)
                except OSError:
                    pass

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            self.query_complete.emit(
                False, f"Codex CLI (exit {proc.returncode}): {detail}")
            return
        self.query_complete.emit(
            True, answer or proc.stdout.strip() or "(Codex no devolvió texto)")
