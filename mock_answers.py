"""Respuestas enlatadas del backend simulado (mock_llm). Imitan el formato
que piden los prompts: Luna = «Di ahora» + viñetas; Sol = secciones ###
con números, tabla, diagrama mermaid y seguimientos."""

import re

import prompts

LUNA = {
    "rate": (
        "**Di ahora:** Propongo un token bucket por API key y por IP, "
        "evaluado en el edge con Redis como contador compartido; asumo "
        "50M DAU, ~6k QPS medios y picos de 3× (≈20k QPS).\n\n"
        "- Token bucket: ráfagas controladas y O(1) por petición.\n"
        "- Script Lua en Redis: leer, rellenar y decrementar atómico.\n"
        "- Clave `rl:{api_key}:{endpoint}`, TTL = ventana × 2.\n"
        "- Cabeceras `X-RateLimit-Remaining` y `Retry-After` al cliente.\n"
        "- Plan: requisitos → cifras → API → arquitectura → hot keys."),
    "scale": (
        "**Di ahora:** Shardeo Redis Cluster por API key con hashing "
        "consistente, réplica por shard y, para hot keys, divido el "
        "cupo entre N sub-claves locales con sincronización asíncrona.\n\n"
        "- Hot key: `rl:{key}#{n}` con n = 8-16 y cupo/N por nodo.\n"
        "- Multi-región: contadores locales + reconciliación cada 100 ms.\n"
        "- Acepto sobre-admisión ≤ 5 % antes que añadir un salto de red.\n"
        "- Failover: Sentinel/Cluster, promover réplica en < 10 s."),
    "fail": (
        "**Di ahora:** Fallo abierto pero con red de seguridad: cada nodo "
        "aplica un límite local conservador (≈ 50 % del cupo) mientras "
        "Redis no responde, y lo registro como incidente.\n\n"
        "- Fail closed tiraría el 100 % del tráfico por un 30 s de caché.\n"
        "- Circuit breaker: 3 timeouts → modo local 30 s → reintento.\n"
        "- Métrica `ratelimit_degraded_total` dispara alerta."),
    "code": (
        "**Di ahora:** Guardo los timestamps en un deque y descarto por la "
        "izquierda los que quedan fuera de la ventana; cada petición es "
        "O(1) amortizado.\n\n"
        "```python\nfrom collections import deque\n\n"
        "class SlidingWindow:\n"
        "    def __init__(self, limit, window_s=60):\n"
        "        self.limit, self.window = limit, window_s\n"
        "        self.hits = deque()\n\n"
        "    def allow(self, now):\n"
        "        while self.hits and now - self.hits[0] >= self.window:\n"
        "            self.hits.popleft()\n"
        "        if len(self.hits) < self.limit:\n"
        "            self.hits.append(now)\n"
        "            return True\n"
        "        return False\n```\n"
        "Complejidad: O(1) amortizado por llamada, O(limit) de memoria."),
    "behave": (
        "**Di ahora:** En un sistema de pagos, producto quería lanzar "
        "reintentos automáticos sin idempotencia; mostré el riesgo de doble "
        "cobro con datos y propuse claves de idempotencia + rollout al 5 %.\n\n"
        "- Situación: reintentos sin idempotencia → doble cargo.\n"
        "- Acción: prototipo en 2 días con `Idempotency-Key`.\n"
        "- Resultado: 0 duplicados en el 5 %, lanzamiento una semana después."),
    "monitor": (
        "**Di ahora:** Mido latencia de decisión p99 < 5 ms, tasa de 429 "
        "por cliente y el porcentaje de tiempo en modo degradado; me "
        "despierta el SLO de disponibilidad del limitador, no un pico de 429.\n\n"
        "- SLO: 99,95 % de decisiones correctas en < 5 ms.\n"
        "- Alertas: `redis_errors`, `degraded_mode > 1 min`, skew entre regiones.\n"
        "- Dashboards por cliente para soporte."),
    "generic": (
        "**Di ahora:** Acoto el alcance a los dos casos de uso principales, "
        "asumo 10M DAU y empiezo por requisitos y cifras antes de dibujar.\n\n"
        "- Requisitos funcionales y no funcionales (p99, disponibilidad).\n"
        "- Estimación: QPS medio/pico, almacenamiento, caché 80/20.\n"
        "- API y modelo de datos con clave de partición.\n"
        "- Arquitectura: CDN → gateway → servicios → caché → BD → colas."),
}

TOPICS = [
    ("code", r"write a function|function that|python|sql|complexity|"
             r"escribe (una )?función|código"),
    ("behave", r"tell me about a time|push back|conflict|cuéntame|"
               r"comportamiento"),
    ("monitor", r"monitor|slo|metrics|observab|alert"),
    ("fail", r"goes down|fail open|fail closed|outage|se cae"),
    ("scale", r"scale|shard|hot key|regions|escal"),
    ("rate", r"rate limit|limitador|throttl|quota"),
]


def _question(kwargs):
    """Última intervención del entrevistador según la cola dinámica."""
    items = kwargs.get("input", [])
    text = ""
    for item in reversed(items):
        if item.get("role") == "user":
            content = item.get("content", "")
            text = content if isinstance(content, str) else "".join(
                b.get("text", "") for b in content)
            break
    lines = [l for l in text.splitlines() if l.startswith("Entrevistador:")]
    return (lines[-1] if lines else text).lower()


def topic_for(kwargs):
    question = _question(kwargs)
    for name, pattern in TOPICS:
        if re.search(pattern, question):
            return name
    return "generic"


SOL_NUMBERS = (
    "### Números\n"
    "- 50M DAU × 20 req/día ÷ 86.400 s ≈ **11,6k QPS** medios; pico 3× ≈ "
    "**35k QPS**.\n"
    "- Cada decisión = 1 EVAL Redis ≈ 0,2 ms CPU → 35k QPS caben en "
    "**2-3 shards** con margen; por HA uso 6 shards + réplica.\n"
    "- Claves activas ≈ 50M × 2 ventanas × ~60 B ≈ **6 GB** en memoria.\n"
    "- Latencia: edge → Redis en la misma AZ ≈ 0,5 ms; p99 < 5 ms viable.\n\n")

SOL_TABLE = (
    "| Algoritmo | Memoria | Precisión | Ráfagas |\n"
    "| --- | --- | --- | --- |\n"
    "| Sliding log | O(n) por clave | exacta | no |\n"
    "| Sliding counter | O(1) | aprox. ±1 % | parcial |\n"
    "| Token bucket | O(1) | buena | sí, controladas |\n\n")

SOL_DIAGRAM = (
    "### Diagrama\n"
    "```mermaid\nflowchart TD\n"
    "  client([Cliente]) -->|HTTPS| cdn[CDN / Edge]\n"
    "  cdn --> gw[API Gateway]\n"
    "  gw -->|EVAL Lua| rl[Rate Limiter]\n"
    "  rl -->|contador| redis[(Redis Cluster)]\n"
    "  rl -->|permitido| svc[Servicios]\n"
    "  rl -.->|429 métricas| kafka{{Kafka}}\n"
    "  kafka -.-> analytics[(Analytics)]\n"
    "  redis -.->|replicación| redis2[(Réplica regional)]\n"
    "```\n\n")

SOL_FOLLOWUPS = (
    "### Si te preguntan…\n"
    "- **¿Por qué no en el gateway con memoria local?** Cupos por cliente "
    "requieren una vista global; local solo como fallback.\n"
    "- **¿Cómo evitas la tormenta de reintentos tras un 429?** `Retry-After` "
    "con jitter y backoff exponencial documentado en el SDK.\n"
    "- **¿Exactitud entre regiones?** Reconciliación asíncrona; acepto "
    "≤ 5 % de sobre-admisión, lo digo como trade-off explícito.")

SOL = {
    "default": (
        "### Detalles\n"
        "- Token bucket por `{api_key, endpoint}`; relleno perezoso "
        "calculado en el script Lua a partir de `last_refill`.\n"
        "- Clave de partición = API key: todas las ventanas de un cliente "
        "caen en el mismo shard, una sola ida y vuelta.\n"
        "- Hot key: dividir el cupo en 16 sub-buckets `#n` y sumar de forma "
        "perezosa; evita que un cliente enorme sature un shard.\n"
        "- Decisión en el gateway, no en cada servicio: un solo punto de "
        "políticas y de métricas.\n\n"
        + SOL_TABLE + SOL_NUMBERS + SOL_DIAGRAM + SOL_FOLLOWUPS),
    "code": (
        "### Detalles\n"
        "- El deque es exacto pero O(limit) de memoria por clave; para "
        "millones de claves usa sliding counter (dos buckets ponderados).\n"
        "- Para multihilo, protege `allow` con un lock o hazlo por shard.\n\n"
        "```python\ndef allow_counter(now, prev, curr, limit, window=60):\n"
        "    frac = (now % window) / window\n"
        "    est = prev * (1 - frac) + curr\n"
        "    return est < limit\n```\n\n"
        "### Números\n- Deque: 60 req/min × 8 B ≈ 480 B por clave; "
        "50M claves ≈ 24 GB → demasiado, de ahí el contador.\n\n"
        "### Si te preguntan…\n"
        "- **¿Y en Redis?** `ZADD` + `ZREMRANGEBYSCORE` + `ZCARD` en un MULTI.\n"
        "- **¿Reloj monótono?** Sí, `time.monotonic()` en un solo proceso; "
        "en distribuido, el reloj de Redis (`TIME`)."),
    "behave": (
        "### Detalles\n"
        "- Cuantifica el riesgo: «1 de cada 2.000 reintentos acababa en "
        "doble cargo en staging».\n"
        "- Muestra la alternativa, no solo el no: idempotencia + flag.\n\n"
        "### Si te preguntan…\n"
        "- **¿Qué habrías hecho si producto insiste?** Escalar con datos y "
        "un plan de rollback en 1 clic."),
}

CORRECTION = (
    "### ⚠ Corrección\n"
    "El entrevistador preguntó por **{question}**, no por lo que la "
    "respuesta rápida contestó; reconduce con: «Déjame volver a lo que "
    "preguntabas sobre {short}».\n\n")

DEEPER_HEADER = (
    "**Di ahora:** Voy a estructurarlo mejor: requisitos, cifras, "
    "algoritmo, escalado y fallos, en ese orden.\n\n")

DIAGRAM_ONLY = (
    "**Di ahora:** Esta es la arquitectura que llevamos hasta ahora.\n\n"
    + SOL_DIAGRAM)


def answer_for(kind, kwargs):
    topic = topic_for(kwargs)
    if kind in ("fast", ""):
        return LUNA.get(topic, LUNA["generic"])
    if kind == "diagram":
        return DIAGRAM_ONLY
    detail = SOL.get(topic, SOL["default"])
    if kind == "detail":
        full_tail = _tail_text(kwargs)
        if prompts.VERIFIED_MATERIAL_NOTE[:30] in full_tail:
            question = _verified_question(full_tail)
            detail = CORRECTION.format(
                question=question, short=question[:40]) + detail
        if prompts.FAST_ANSWER_FAILED in full_tail:
            detail = LUNA.get(topic, LUNA["generic"]) + "\n\n" + detail
        return detail
    if kind == "deeper":
        return DEEPER_HEADER + LUNA.get(topic, LUNA["generic"]).split(
            "\n\n", 1)[-1] + "\n\n" + detail
    return detail


def _tail_text(kwargs):
    parts = []
    for item in kwargs.get("input", [])[1:]:
        content = item.get("content", "")
        parts.append(content if isinstance(content, str) else "".join(
            b.get("text", "") for b in content))
    return "\n".join(parts)


def _verified_question(text):
    match = re.search(r"«([^»]+)»", text)
    return match.group(1) if match else "la pregunta corregida"


def arbiter_for(text):
    """Árbitro simulado: corrige «rite limiter» → «rate limiter» cuando la
    segunda transcripción lo dice; si no, verifica sin cambios."""
    pairs = re.findall(r"A: (.*)\n\s+B: (.*)", text)
    changed = any(a.strip().lower() != b.strip().lower()
                  for a, b in pairs if b and "(sin" not in b)
    question = (pairs[-1][1] if pairs and pairs[-1][1]
                and "(sin" not in pairs[-1][1]
                else (pairs[-1][0] if pairs else "")).strip()
    material = changed and any(
        w in question.lower() for w in ("rate", "scale", "fail", "shard"))
    return {"question": question or "¿Pregunta?", "changed": changed,
            "material": material,
            "corrections": [f"{a} → {b}" for a, b in pairs
                            if b and a.strip() != b.strip()][:3]}
