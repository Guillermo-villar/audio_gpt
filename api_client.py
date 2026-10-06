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


def clef_question(text, timeout=4.0):
    """Gate real con @cf/cloudflare/clef-flash (Workers AI, neuronas).

    Devuelve (es_pregunta, probabilidad) o None sin creds/error — en ese
    caso el llamante cae a la heurística looks_like_question.
    """
    token, account = cloudflare_creds()
    if not token or not account:
        return None
    url = (f"https://api.cloudflare.com/client/v4/accounts/{account}"
           "/ai/run/@cf/cloudflare/clef-flash")
    body = {
        "model": "clef-flash",
        "state": text,
        "questions": {
            "is_question": {
                "type": "choice",
                "instructions": (
                    "Is this utterance a question or a task directed at the "
                    "listener? yes only for real questions or requests "
                    "(technical or behavioral), no for small talk, filler, "
                    "ads, or statements not asking anything."),
                "criteria": {
                    "yes": "genuine question or request to answer",
                    "no": "small talk, filler, or no question asked",
                },
            }
        },
    }
    try:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            answers = json.loads(r.read())["result"]["answers"]
        a = answers.get("is_question", {})
        probs = a.get("probabilities") or {}
        p_yes = float(probs.get(
            "yes", 1.0 if a.get("choice") == "yes" else 0.0))
        return (a.get("choice") == "yes" or p_yes > 0.5), p_yes
    except Exception:
        return None


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
    "system_prompt": (
        "Eres un asistente virtual experto que ayuda a los usuarios a responder "
        "preguntas sobre conceptos técnicos y resolver problemas de programación "
        "típicos de entrevistas técnicas. Analiza el texto proporcionado (que viene "
        "de una transcripción de audio, por lo que puede tener errores) y busca en "
        "la transcripción preguntas, aunque no estén explícitamente formuladas. "
        "Por ejemplo, si el texto trata de un problema típico de entrevistas "
        "técnicas estilo leetcode, interpreta que es una pregunta técnica y "
        "devuelve código en Python que lo resuelva.\n\n"
        "1. PREGUNTAS CONCEPTUALES:\n"
        "- Explicaciones claras y concisas de estadística, machine learning o "
        "programación.\n"
        "- Definición, puntos clave y ejemplos cuando sea apropiado.\n"
        "- Responde directamente, sin introducciones largas.\n\n"
        "2. PREGUNTAS DE PROGRAMACIÓN:\n"
        "- Si la pregunta es sobre Python (o no especifica lenguaje), código en "
        "Python limpio y bien comentado.\n"
        "- Si la pregunta es claramente sobre SQL y se pide una consulta, código "
        "SQL optimizado.\n"
        "- Explica brevemente la lógica del código.\n\n"
        "Responde en el mismo idioma de la pregunta (español o inglés). Ignora "
        "texto en otros idiomas por posibles fallos de transcripción. Sé preciso "
        "y directo."
    ),
    "temperature": None,          # los modelos de razonamiento (gpt-5/6) no admiten temperature
    "reasoning_effort": "low",    # respuestas rápidas y baratas; sube a "medium" para más calidad
    "service_tier": "fast",       # Fast mode (~2.5x menos latencia, ~2x precio); "auto"/"flex" alternativas
    "max_tokens": 2000,
}


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
                    effort=None, max_tokens=None, brief=""):
        """Envía la transcripción a GPT y devuelve (ok, respuesta_o_error).

        engine="cloudflare" usa Workers AI (openai/gpt-6-luna servido por CF,
        endpoint compatible con Responses); "openai" la API normal.
        `context` son las últimas intervenciones etiquetadas por carril.
        `effort` sube el razonamiento para la pasada de revisión.
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
                "cf_model", "@cf/meta/llama-3.3-70b-instruct-fp8-fast")
        else:
            client = OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))
            model = config.get("model", DEFAULT_GPT_CONFIG["model"])

        instructions = config.get("system_prompt", "Eres un asistente útil.")
        if brief:
            # El brief es identidad/política (system), no contenido (input):
            # experiencia aprobada + límites explícitos contra fabricación.
            instructions += (
                "\n\nContexto de la entrevista — experiencia APROBADA del "
                "candidato y límites; úsala para adaptar cada respuesta:\n"
                + brief +
                "\nNunca conviertas requisitos del puesto ni notas de empresa "
                "en experiencia del candidato ni inventes métricas o historias "
                "que el brief no respalde; ante falta de evidencia, responde "
                "en hipotético."
            )

        if context:
            gpt_input = (f"Contexto de la conversación:\n{context}\n\n"
                         f"Nueva intervención: {transcription}")
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
        if effort:
            kwargs["reasoning"] = {"effort": effort}
        if (engine != "cloudflare"
                and config.get("service_tier")
                and config["service_tier"] != "auto"):
            kwargs["service_tier"] = config["service_tier"]

        try:
            if engine == "cloudflare" and model.startswith("@cf/"):
                # Workers AI nativo: endpoint de chat completions (el de
                # responses no acepta modelos @cf/*).
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system",
                         "content": kwargs["instructions"]},
                        {"role": "user", "content": kwargs["input"]},
                    ],
                    max_tokens=kwargs["max_output_tokens"],
                )
                text = response.choices[0].message.content
                return (True, text) if text else (False, "La API no devolvió texto")
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


class GptQueryThread(QThread):
    """Hilo para enviar consultas a GPT sin bloquear la interfaz."""

    query_complete = Signal(bool, str)

    def __init__(self, api_key, transcription, engine="openai", context="",
                 effort=None, max_tokens=None, brief=""):
        super().__init__()
        self.api_key = api_key
        self.transcription = transcription
        self.engine = engine
        self.context = context
        self.effort = effort
        self.max_tokens = max_tokens
        self.brief = brief

    def run(self):
        success, result = GptClient.send_to_gpt(
            self.api_key, self.transcription, engine=self.engine,
            context=self.context, effort=self.effort,
            max_tokens=self.max_tokens, brief=self.brief)
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
                 brief=""):
        super().__init__()
        self.transcription = transcription
        self.model = model
        self.context = context
        self.brief = brief

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

        config = GptClient.load_config() or dict(DEFAULT_GPT_CONFIG)
        prompt = config.get("system_prompt", "Eres un asistente útil.")
        if self.brief:
            prompt += (
                "\n\nContexto de la entrevista — experiencia APROBADA del "
                "candidato y límites; úsala para adaptar cada respuesta:\n"
                + self.brief +
                "\nNunca conviertas requisitos del puesto ni notas de empresa "
                "en experiencia del candidato ni inventes métricas o historias "
                "que el brief no respalde; ante falta de evidencia, responde "
                "en hipotético."
            )
        if self.context:
            prompt += f"\n\nContexto de la conversación:\n{self.context}"
        prompt += "\n\nTranscription: " + self.transcription

        out_path = None
        try:
            with tempfile.NamedTemporaryFile(
                    suffix=".txt", delete=False) as tf:
                out_path = tf.name
            proc = subprocess.run(
                [exe, "exec", "--skip-git-repo-check",
                 "--sandbox", "read-only",            # nunca edita archivos
                 "--output-last-message", out_path,   # solo la respuesta final
                 "-m", self.model, prompt],
                capture_output=True, text=True, timeout=300,
                encoding="utf-8", errors="replace",
            )
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
