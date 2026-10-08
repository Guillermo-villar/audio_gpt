
F1 = """flowchart TD
  user([Usuario]) -->|GET /abc123| cdn[CDN]
  cdn -->|miss| lb[Load Balancer]
  lb --> api[API stateless]
  subgraph datos [Capa de datos]
    cache[(Redis)]
    db[(Postgres shards)]
  end
  api -->|lookup| cache
  cache -.->|miss| db
  api -->|POST /shorten| idgen[ID Generator]
  idgen --> db
  api -.->|evento click| q{{Kafka}}
  q -.-> analytics[Analytics workers]
  analytics --> olap[(ClickHouse)]"""

F2 = """graph LR
  client([Cliente]) --> gw[API Gateway]
  gw -->|check token bucket| rl[Rate Limiter]
  rl <-->|INCR + TTL| redis[(Redis cluster)]
  rl -->|allowed| svc[Servicio]
  rl -->|429| client
  cfg[Config service] -.-> rl"""

F3 = """flowchart TD
  A[Post Service] -->|nuevo post| K{{Kafka: posts}}
  K -.-> F[Fan-out workers]
  F -->|push ids| T[(Timeline cache)]
  F -.->|celebrities: pull| R[Read path]
  U([Usuario]) --> G[Feed API]
  G --> T
  G --> R
  R --> P[(Posts DB)]
  G --> M[Media CDN]"""

SEQUENCE = """sequenceDiagram
  Alice->>Bob: Hello
  Bob-->>Alice: Hi"""

GARBAGE = "esto no es mermaid\n??? -> !!!"

SOL_DETAIL_PATH = (
    r"C:\Users\guill\AppData\Local\Temp\audio_gpt_probes\sol_detail.md")


def sol_detail_diagram():
    try:
        with open(SOL_DETAIL_PATH, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    start = text.find("```mermaid")
    if start < 0:
        return None
    start = text.index("\n", start) + 1
    return text[start:text.index("```", start)].strip("\n")


def estimate_measure(label, max_w):
    """Medida por caracteres (sin Qt) para tests de layout."""
    width = 0.0
    height = 0.0
    for line in label.split("\n"):
        chars_per_line = max(1, int(max_w // 6.4))
        count = max(1, -(-len(line) // chars_per_line))
        width = max(width, min(max_w, len(line) * 6.4))
        height += count * 15.0
    return width, height


ALTW = """flowchart TD
    client([Clients]) -->|HTTP| lb[Load balancer]
    lb -->|route| app[Stateless app servers]
    app -->|lookup| cache[Cache]
    cache -->|cache miss| db[(DynamoDB or Cassandra)]
    db -.->|populate| cache
    app -.->|allocate ID ranges| ids[ID counter service]"""
