"""Mermaid (flowchart) → grafo → layout por capas. Python puro, sin Qt.

split_mermaid separa los bloques ```mermaid del markdown; parse_mermaid
entiende el subconjunto de flowchart/graph que usa el copiloto; layout_graph
coloca nodos, aristas y subgrafos (Sugiyama simplificado) con una función
`measure` inyectada para el tamaño del texto.
"""

import functools
import re
from dataclasses import dataclass, field


@dataclass
class Node:
    id: str
    label: str
    shape: str = "rect"


@dataclass
class Edge:
    src: str
    dst: str
    label: str = ""
    style: str = "solid"
    head: bool = True
    tail: bool = False


@dataclass
class Group:
    id: str
    title: str
    members: list
    parent: str = ""


@dataclass
class Graph:
    direction: str = "TD"
    nodes: dict = field(default_factory=dict)
    edges: list = field(default_factory=list)
    groups: list = field(default_factory=list)


# ------------------------------ split ------------------------------------

_FENCE_OPEN = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*mermaid\s*$", re.I)
_FENCE_ANY = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


def _closes(line, fence):
    s = line.strip()
    return bool(s) and set(s) == {fence[0]} and len(s) >= len(fence)


def split_mermaid(markdown, accept=None):
    lines = (markdown or "").split("\n")
    out = []
    closed = []
    has_open = False
    changed = False
    need_sep = False
    i = 0
    n = len(lines)

    def emit(line):
        nonlocal need_sep
        if need_sep:
            if not line.strip():
                return
            out.append("")
            need_sep = False
        out.append(line)

    while i < n:
        line = lines[i]
        m = _FENCE_OPEN.match(line)
        if m:
            fence = m.group(1)
            j = i + 1
            while j < n and not _closes(lines[j], fence):
                j += 1
            if j >= n:
                has_open = True
                changed = True
                break
            source = "\n".join(lines[i + 1:j])
            if accept is None or accept(source):
                closed.append(source)
                changed = True
                while out and not out[-1].strip():
                    out.pop()
                need_sep = bool(out)
            else:
                for k in range(i, j + 1):
                    emit(lines[k])
            i = j + 1
            continue
        m = _FENCE_ANY.match(line)
        if m:
            fence = m.group(1)
            j = i + 1
            while j < n and not _closes(lines[j], fence):
                j += 1
            for k in range(i, min(j, n - 1) + 1):
                emit(lines[k])
            i = j + 1
            continue
        emit(line)
        i += 1
    if not changed:
        return markdown or "", closed, has_open
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out), closed, has_open


_HEADING = re.compile(r"^\s{0,3}#{1,6}(?:\s|$)")


def drop_empty_headings(markdown):
    """Quita encabezados Markdown sin contenido (seguidos solo de líneas en
    blanco y otro encabezado o el final), p. ej. tras extraer un diagrama."""
    lines = (markdown or "").split("\n")
    is_heading = []
    fence = None
    for line in lines:
        if fence:
            is_heading.append(False)
            if _closes(line, fence):
                fence = None
            continue
        m = _FENCE_ANY.match(line)
        if m:
            fence = m.group(1)
            is_heading.append(False)
            continue
        is_heading.append(bool(_HEADING.match(line)))
    changed = False
    while True:
        for i in range(len(lines)):
            if not is_heading[i]:
                continue
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j >= len(lines) or is_heading[j]:
                del lines[i]
                del is_heading[i]
                if i < len(lines) and not lines[i].strip() and (
                        i == 0 or not lines[i - 1].strip()):
                    del lines[i]
                    del is_heading[i]
                changed = True
                break
        else:
            break
    if not changed:
        return markdown
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines)


# ------------------------------ parse ------------------------------------

_NODE_ID = re.compile(r"\w+(?:-(?![-.>])\w+)*")
_HEADER = re.compile(
    r"^(?:flowchart|graph)(?:-elk)?(?:\s+(TD|TB|BT|LR|RL))?\s*$", re.I)
_IGNORED = re.compile(
    r"^(classDef|class|style|linkStyle|click|direction|accTitle|accDescr)"
    r"(?=[\s:]|$)")
_SUBGRAPH = re.compile(r"^subgraph\b\s*(.*)$")
_SHAPES = (
    ("(((", (")))",), "circle"),
    ("((", ("))",), "circle"),
    ("([", ("])",), "stadium"),
    ("(", (")",), "round"),
    ("[[", ("]]",), "subroutine"),
    ("[(", (")]",), "db"),
    ("[/", ("/]", "\\]"), "para"),
    ("[\\", ("\\]", "/]"), "para"),
    ("[", ("]",), "rect"),
    ("{{", ("}}",), "hexagon"),
    ("{", ("}",), "diamond"),
    (">", ("]",), "asym"),
)
_INLINE = (
    (re.compile(r"(<)?--\s+(.+?)\s+(-{2,}>|-{3,}|--[ox])(?=\s|$)"), "solid"),
    (re.compile(r"(<)?-\.\s+(.+?)\s+\.-(>)?(?=\s|$)"), "dotted"),
    (re.compile(r"(<)?==\s+(.+?)\s+(={2,}>|={3,})(?=\s|$)"), "thick"),
)
_DOTTED = re.compile(r"(<)?-\.+-(>)?")
_THICK = re.compile(r"(<)?={2,}(>)?")
_SOLID = re.compile(r"(<)?-{2,}(?:(>)|([ox])(?=\s))?")
_CLASS_SUFFIX = re.compile(r":::[\w-]+")


def _clean(label):
    t = label.strip()
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        t = t[1:-1]
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = t.replace("`", "").replace("**", "")
    return "\n".join(
        re.sub(r"[ \t]+", " ", part).strip() for part in t.split("\n")
    ).strip()


def _skip_spaces(s, i):
    while i < len(s) and s[i] in " \t":
        i += 1
    return i


def _read_label(s, p, closers):
    k = _skip_spaces(s, p)
    if k < len(s) and s[k] == '"':
        q = s.find('"', k + 1)
        if q < 0:
            return None
        text = s[k + 1:q]
        k = _skip_spaces(s, q + 1)
        for closer in closers:
            if s.startswith(closer, k):
                return _clean(text), k + len(closer)
        return None
    best = None
    for closer in closers:
        pos = s.find(closer, p)
        if pos >= 0 and (best is None or pos < best[0]):
            best = (pos, closer)
    if best is None:
        return None
    return _clean(s[p:best[0]]), best[0] + len(best[1])


def _parse_node(s, i):
    m = _NODE_ID.match(s, i)
    if not m:
        return None
    node_id = m.group()
    j = m.end()
    label = shape = None
    for opener, closers, name in _SHAPES:
        if s.startswith(opener, j):
            found = _read_label(s, j + len(opener), closers)
            if found:
                label, j = found
                shape = name
                break
    if s.startswith(":::", j):
        m2 = _CLASS_SUFFIX.match(s, j)
        j = m2.end() if m2 else j + 3
    return node_id, label, shape, j


def _parse_group(s, i):
    first = _parse_node(s, i)
    if first is None:
        return None
    nodes = [first[:3]]
    j = first[3]
    while True:
        k = _skip_spaces(s, j)
        if k < len(s) and s[k] == "&":
            nxt = _parse_node(s, _skip_spaces(s, k + 1))
            if nxt is None:
                return None
            nodes.append(nxt[:3])
            j = nxt[3]
        else:
            return nodes, j


def _parse_connector(s, i):
    style = head = tail = label = None
    j = i
    for rx, name in _INLINE:
        m = rx.match(s, i)
        if m:
            tail = bool(m.group(1))
            label = _clean(m.group(2))
            closer = m.group(3)
            head = bool(closer) and (closer.endswith(">") or closer[-1] in "ox")
            if name == "dotted":
                head = bool(m.group(3))
            style = name
            j = m.end()
            break
    else:
        m = _DOTTED.match(s, i)
        if m:
            style, tail, head = "dotted", bool(m.group(1)), bool(m.group(2))
        else:
            m = _THICK.match(s, i)
            if m:
                style, tail, head = "thick", bool(m.group(1)), bool(m.group(2))
            else:
                m = _SOLID.match(s, i)
                if not m:
                    return None
                style, tail = "solid", bool(m.group(1))
                head = bool(m.group(2) or m.group(3))
        j = m.end()
    k = _skip_spaces(s, j)
    if k < len(s) and s[k] == "|":
        q = s.find("|", k + 1)
        if q > k:
            label = _clean(s[k + 1:q])
            j = q + 1
    return style, head, tail, label or "", j


def _parse_statement(s):
    first = _parse_group(s, 0)
    if first is None:
        return None
    items = [first[0]]
    conns = []
    j = first[1]
    while True:
        k = _skip_spaces(s, j)
        if k >= len(s):
            return items, conns
        conn = _parse_connector(s, k)
        if conn is None:
            return None
        style, head, tail, label, k2 = conn
        group = _parse_group(s, _skip_spaces(s, k2))
        if group is None:
            return None
        conns.append((style, head, tail, label))
        items.append(group[0])
        j = group[1]


def _split_statements(source):
    lines = source.replace("\r", "").split("\n")
    if lines and lines[0].strip() == "---":
        for idx in range(1, len(lines)):
            if lines[idx].strip() == "---":
                lines = lines[idx + 1:]
                break
    statements = []
    for line in lines:
        buf = []
        quote = False
        depth = 0
        for ch in line:
            if ch == '"':
                quote = not quote
            elif not quote:
                if ch in "([{":
                    depth += 1
                elif ch in ")]}":
                    depth = max(0, depth - 1)
                elif ch == ";" and depth == 0:
                    statements.append("".join(buf))
                    buf = []
                    continue
            buf.append(ch)
        statements.append("".join(buf))
    result = []
    for stmt in statements:
        stmt = stmt.strip()
        if stmt and not stmt.startswith("%%"):
            result.append(stmt)
    return result


def _slug(text):
    return re.sub(r"\W+", "_", text.strip()) or "group"


def _parse_subgraph(rest):
    rest = rest.strip()
    m = re.match(r"^([^\s\[\"]+)\s*\[(.*)\]$", rest)
    if m:
        return m.group(1), _clean(m.group(2))
    if rest.startswith('"') and rest.endswith('"') and len(rest) >= 2:
        title = _clean(rest)
        return _slug(title), title
    return _slug(rest), _clean(rest)


def _parse(source):
    statements = _split_statements(source)
    if not statements:
        return None
    header = _HEADER.match(statements[0])
    if not header:
        return None
    direction = (header.group(1) or "TD").upper()
    direction = {"TB": "TD", "BT": "TD", "RL": "LR"}.get(direction, direction)
    graph = Graph(direction=direction)
    groups = {}
    stack = []

    def add_node(node_id, label, shape):
        node = graph.nodes.get(node_id)
        if node is None:
            node = Node(node_id, label or node_id, shape or "rect")
            graph.nodes[node_id] = node
            if stack:
                groups[stack[-1]].members.append(node_id)
        else:
            if label:
                node.label = label
            if shape:
                node.shape = shape

    for stmt in statements[1:]:
        try:
            if _IGNORED.match(stmt):
                continue
            sub = _SUBGRAPH.match(stmt)
            if sub:
                gid, title = _parse_subgraph(sub.group(1))
                base, n = gid, 2
                while gid in groups:
                    gid = f"{base}_{n}"
                    n += 1
                group = Group(gid, title or gid, [],
                              stack[-1] if stack else "")
                groups[gid] = group
                graph.groups.append(group)
                stack.append(gid)
                continue
            if stmt.lower() == "end":
                if stack:
                    stack.pop()
                continue
            parsed = _parse_statement(stmt)
            if parsed is None:
                continue
            items, conns = parsed
            for nodes in items:
                for node_id, label, shape in nodes:
                    add_node(node_id, label, shape)
            for idx, (style, head, tail, label) in enumerate(conns):
                for a in items[idx]:
                    for b in items[idx + 1]:
                        graph.edges.append(Edge(
                            a[0], b[0], label, style, head, tail))
        except Exception:
            continue
    if not graph.nodes:
        return None
    return graph


@functools.lru_cache(maxsize=256)
def _parse_cached(source):
    try:
        return _parse(source)
    except Exception:
        return None


def parse_mermaid(source):
    if not isinstance(source, str):
        return None
    return _parse_cached(source)


# ------------------------------ layout -----------------------------------

MARGIN = 12
GROUP_PAD = 10
GROUP_TITLE_BAND = 16
VIRTUAL_GAP = 10


@dataclass
class NodeBox:
    id: str
    label: str
    shape: str
    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self):
        return self.x + self.w / 2

    @property
    def cy(self):
        return self.y + self.h / 2


@dataclass
class EdgePath:
    src: str
    dst: str
    label: str
    style: str
    head: bool
    tail: bool
    points: list
    label_pos: tuple = None
    label_size: tuple = (0.0, 0.0)
    back: bool = False


@dataclass
class GroupBox:
    id: str
    title: str
    x: float
    y: float
    w: float
    h: float
    parent: str = ""


@dataclass
class Layout:
    width: float
    height: float
    direction: str
    nodes: dict
    edges: list
    groups: list


def _node_size(node, measure, max_label_w):
    tw, th = measure(node.label, max_label_w)
    w = max(64.0, tw + 20.0)
    h = max(30.0, th + 14.0)
    shape = node.shape
    if shape == "db":
        h += 10
    elif shape == "diamond":
        w *= 1.35
        h *= 1.5
    elif shape == "circle":
        w = h = max(w, h)
    elif shape == "hexagon":
        w += 16
    elif shape == "stadium":
        w += 12
    return float(w), float(h)


def _pav(values):
    blocks = []
    for v in values:
        blocks.append([v, 1])
        while len(blocks) > 1 and (
                blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]):
            s, c = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += c
    out = []
    for s, c in blocks:
        out.extend([s / c] * c)
    return out


def _median(values):
    values = sorted(values)
    n = len(values)
    mid = n // 2
    if n % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def layout_graph(graph, measure, direction="TD", max_label_w=140):
    td = direction != "LR"
    direction = "TD" if td else "LR"
    ids = list(graph.nodes)
    index = {nid: i for i, nid in enumerate(ids)}
    size = {nid: _node_size(graph.nodes[nid], measure, max_label_w)
            for nid in ids}

    # --- jerarquía de subgrafos ---
    gmap = {g.id: g for g in graph.groups}
    node_group = {}
    for g in graph.groups:
        for m in g.members:
            node_group.setdefault(m, g.id)

    def group_path(gid):
        path = []
        while gid:
            path.append(gid)
            gid = gmap[gid].parent if gid in gmap else ""
        return tuple(reversed(path))

    path_of = {nid: group_path(node_group.get(nid, "")) for nid in ids}
    full_members = {}
    for nid, path in path_of.items():
        for gid in path:
            full_members.setdefault(gid, []).append(nid)
    children = {}
    for g in graph.groups:
        if g.parent:
            children.setdefault(g.parent, []).append(g.id)

    # --- aristas, ciclos y rangos ---
    edges = [(i, e) for i, e in enumerate(graph.edges)
             if e.src != e.dst and e.src in index and e.dst in index]
    succ = {nid: [] for nid in ids}
    for _, e in edges:
        if e.dst not in succ[e.src]:
            succ[e.src].append(e.dst)
    state = {}
    back_pairs = set()
    for root in ids:
        if root in state:
            continue
        state[root] = 1
        stack = [(root, iter(succ[root]))]
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                st = state.get(nxt, 0)
                if st == 1:
                    back_pairs.add((node, nxt))
                elif st == 0:
                    state[nxt] = 1
                    stack.append((nxt, iter(succ[nxt])))
                    advanced = True
                    break
            if not advanced:
                state[node] = 2
                stack.pop()
    dag = []
    for u in ids:
        for v in succ[u]:
            pair = (v, u) if (u, v) in back_pairs else (u, v)
            if pair not in dag:
                dag.append(pair)
    preds = {nid: [] for nid in ids}
    dsucc = {nid: [] for nid in ids}
    for a, b in dag:
        dsucc[a].append(b)
        preds[b].append(a)
    indeg = {nid: len(preds[nid]) for nid in ids}
    queue = [nid for nid in ids if indeg[nid] == 0]
    rank = {nid: 0 for nid in ids}
    head = 0
    while head < len(queue):
        node = queue[head]
        head += 1
        for nxt in dsucc[node]:
            rank[nxt] = max(rank[nxt], rank[node] + 1)
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    for nid in ids:
        if not preds[nid] and dsucc[nid]:
            rank[nid] = max(rank[nid], min(rank[s] for s in dsucc[nid]) - 1)
    nranks = max(rank.values()) + 1 if rank else 1

    # --- nodos virtuales para aristas largas ---
    chains = {}
    breadth = {}
    depth = {}
    for nid in ids:
        w, h = size[nid]
        breadth[nid], depth[nid] = (w, h) if td else (h, w)
    xedges = []
    for ei, e in edges:
        back = (e.src, e.dst) in back_pairs
        a, b = (e.dst, e.src) if back else (e.src, e.dst)
        chain = [a]
        for r in range(rank[a] + 1, rank[b]):
            vid = f"~{ei}_{r}"
            rank[vid] = r
            breadth[vid] = 0.0
            depth[vid] = 0.0
            path_of[vid] = ()
            chain.append(vid)
        chain.append(b)
        chains[ei] = (chain, back)
        for k in range(len(chain) - 1):
            xedges.append((chain[k], chain[k + 1]))
    all_nodes = ids + [n for n in rank if n not in index]
    xpreds = {n: [] for n in all_nodes}
    xsuccs = {n: [] for n in all_nodes}
    by_rank = [[] for _ in range(nranks)]
    for a, b in xedges:
        xsuccs[a].append(b)
        xpreds[b].append(a)
        by_rank[rank[a]].append((a, b))

    # --- orden inicial (DFS) ---
    visit = {}
    roots = [n for n in all_nodes if not xpreds[n]] + all_nodes
    for root in roots:
        if root in visit:
            continue
        visit[root] = len(visit)
        stack = [(root, iter(xsuccs[root]))]
        while stack:
            node, it = stack[-1]
            for nxt in it:
                if nxt not in visit:
                    visit[nxt] = len(visit)
                    stack.append((nxt, iter(xsuccs[nxt])))
                    break
            else:
                stack.pop()
    layers = [[] for _ in range(nranks)]
    for n in all_nodes:
        layers[rank[n]].append(n)
    for layer in layers:
        layer.sort(key=lambda n: visit[n])

    def positions(ls):
        return {n: i for layer in ls for i, n in enumerate(layer)}

    def crossings(ls):
        pos = positions(ls)
        total = 0
        for pairs in by_rank:
            ps = [(pos[a], pos[b]) for a, b in pairs]
            for i in range(len(ps)):
                for j in range(i + 1, len(ps)):
                    if (ps[i][0] - ps[j][0]) * (ps[i][1] - ps[j][1]) < 0:
                        total += 1
        return total

    def arrange(nodes, level, keys, pos, gkeys):
        lone = []
        grouped = {}
        for n in nodes:
            path = path_of.get(n, ())
            if len(path) > level:
                grouped.setdefault(path[level], []).append(n)
            else:
                lone.append(n)
        units = [(keys[n], pos[n], [n]) for n in lone]
        for gid, members in grouped.items():
            units.append((gkeys[gid], min(pos[m] for m in members),
                          arrange(members, level + 1, keys, pos, gkeys)))
        units.sort(key=lambda u: (u[0], u[1]))
        return [n for unit in units for n in unit[2]]

    best = [list(layer) for layer in layers]
    best_cross = crossings(layers)
    for it in range(8):
        if best_cross == 0:
            break
        down = it % 2 == 0
        order = range(1, nranks) if down else range(nranks - 2, -1, -1)
        for r in order:
            pos = positions(layers)
            keys = {}
            for n in all_nodes:
                nb = xpreds[n] if down else xsuccs[n]
                keys[n] = (sum(pos[x] for x in nb) / len(nb)) if nb \
                    else float(pos[n])
            gkeys = {}
            for gid, members in full_members.items():
                gkeys[gid] = sum(keys[m] for m in members) / len(members)
            layers[r] = arrange(layers[r], 0, keys, pos, gkeys)
        cross = crossings(layers)
        if cross < best_cross:
            best_cross = cross
            best = [list(layer) for layer in layers]
    layers = best

    # --- etiquetas de aristas por hueco entre rangos ---
    label_sizes = {}
    gap_label = {}
    for ei, e in edges:
        if e.label:
            lw, lh = measure(e.label, max_label_w)
            label_sizes[ei] = (lw + 8.0, lh + 4.0)
            chain, _ = chains[ei]
            seg = (len(chain) - 2) // 2
            a = chain[0]
            g = rank[a] + seg
            gap_label[g] = max(gap_label.get(g, 0.0), label_sizes[ei][0])

    # --- coordenadas: eje de rangos (v) ---
    rank_depth = [max([depth[n] for n in layer] or [0.0]) for layer in layers]
    vc = []
    start = 0.0
    for r in range(nranks):
        vc.append(start + rank_depth[r] / 2)
        if td:
            gap = 40.0 + (12.0 if r in gap_label else 0.0)
        else:
            gap = max(56.0, gap_label[r] + 16.0) if r in gap_label else 56.0
        start += rank_depth[r] + gap

    nodesep = 18.0 if td else 14.0

    def minsep(a, b):
        sep = breadth[a] / 2 + breadth[b] / 2
        if a in index and b in index:
            sep += nodesep
            sep += 12.0 * len(set(path_of[a]) ^ set(path_of[b]))
        else:
            sep += VIRTUAL_GAP
        return sep

    u = {}
    widths = []
    for layer in layers:
        x = 0.0
        for i, n in enumerate(layer):
            x = breadth[n] / 2 if i == 0 else x + minsep(layer[i - 1], n)
            u[n] = x
        widths.append((u[layer[-1]] + breadth[layer[-1]] / 2) if layer
                      else 0.0)
    widest = max(widths) if widths else 0.0
    for layer, wdt in zip(layers, widths):
        for n in layer:
            u[n] += (widest - wdt) / 2

    def align(r, neighbours):
        layer = layers[r]
        if not layer:
            return
        offsets = [0.0]
        for i in range(1, len(layer)):
            offsets.append(offsets[-1] + minsep(layer[i - 1], layer[i]))
        desired = []
        for i, n in enumerate(layer):
            nb = neighbours[n]
            d = _median([u[x] for x in nb]) if nb else u[n]
            desired.append(d - offsets[i])
        for n, z, off in zip(layer, _pav(desired), offsets):
            u[n] = z + off

    for sweep in range(3):
        if sweep % 2 == 0:
            for r in range(1, nranks):
                align(r, xpreds)
        else:
            for r in range(nranks - 2, -1, -1):
                align(r, xsuccs)

    # --- rectángulos reales ---
    title_w = {}
    for g in graph.groups:
        tw, _ = measure(g.title, 10000)
        title_w[g.id] = tw + 14.0

    def node_rects():
        rects = {}
        for nid in ids:
            w, h = size[nid]
            c = vc[rank[nid]]
            if td:
                rects[nid] = (u[nid] - w / 2, c - h / 2, w, h)
            else:
                rects[nid] = (c - w / 2, u[nid] - h / 2, w, h)
        return rects

    group_order = sorted(
        gmap, key=lambda gid: -len(group_path(gid)))

    def group_rects(rects):
        out = {}
        for gid in group_order:
            parts = [rects[m] for m in full_members.get(gid, [])]
            parts += [out[c] for c in children.get(gid, []) if c in out]
            if not parts:
                continue
            x0 = min(p[0] for p in parts) - GROUP_PAD
            y0 = min(p[1] for p in parts) - GROUP_PAD - GROUP_TITLE_BAND
            x1 = max(p[0] + p[2] for p in parts) + GROUP_PAD
            y1 = max(p[1] + p[3] for p in parts) + GROUP_PAD
            x1 = max(x1, x0 + title_w[gid])
            out[gid] = (x0, y0, x1 - x0, y1 - y0)
        return out

    def u_span(rect):
        return (rect[0], rect[0] + rect[2]) if td else (rect[1], rect[1] + rect[3])

    for _ in range(80):
        rects = node_rects()
        grects = group_rects(rects)
        violation = None
        for gid, grect in grects.items():
            members = set(full_members.get(gid, []))
            for nid in ids:
                if nid in members:
                    continue
                nx, ny, nw, nh = rects[nid]
                if (nx < grect[0] + grect[2] and nx + nw > grect[0]
                        and ny < grect[1] + grect[3] and ny + nh > grect[1]):
                    violation = (gid, nid, grect)
                    break
            if violation:
                break
        if not violation:
            break
        gid, nid, grect = violation
        layer = layers[rank[nid]]
        idx = layer.index(nid)
        member_idx = [i for i, n in enumerate(layer)
                      if n in full_members.get(gid, ())]
        g0, g1 = u_span(grect)
        n0, n1 = u_span(rects[nid])
        if member_idx:
            right = idx > min(member_idx)
        else:
            right = (n0 + n1) / 2 >= (g0 + g1) / 2
        if right:
            delta = g1 + 8 - n0
            for n in layer[idx:]:
                u[n] += delta
        else:
            delta = n1 - (g0 - 8)
            for n in layer[:idx + 1]:
                u[n] -= delta

    rects = node_rects()
    grects = group_rects(rects)

    # --- aristas ---
    def centre(n):
        return (u[n], vc[rank[n]]) if td else (vc[rank[n]], u[n])

    def out_pt(n, au=None):
        au = u[n] if au is None else au
        if td:
            return (au, vc[rank[n]] + depth[n] / 2)
        return (vc[rank[n]] + depth[n] / 2, au)

    def in_pt(n, au=None):
        au = u[n] if au is None else au
        if td:
            return (au, vc[rank[n]] - depth[n] / 2)
        return (vc[rank[n]] - depth[n] / 2, au)

    out_ends = {}
    in_ends = {}
    for ei, _ in edges:
        chain = chains[ei][0]
        out_ends.setdefault(chain[0], []).append((u[chain[1]], ei))
        in_ends.setdefault(chain[-1], []).append((u[chain[-2]], ei))

    def spread(ends_by_node):
        anchors = {}
        for n, ends in ends_by_node.items():
            ends.sort()
            if len(ends) == 1:
                anchors[ends[0][1]] = u[n]
                continue
            lo, span = ((rects[n][0], rects[n][2]) if td
                        else (rects[n][1], rects[n][3]))
            for i, (_, ei) in enumerate(ends):
                anchors[ei] = lo + span * (0.2 + 0.6 * i / (len(ends) - 1))
        return anchors

    start_u = spread(out_ends)
    end_u = spread(in_ends)

    placed = []
    node_obstacles = list(rects.values())

    def curve_point(p, q, t):
        along = 1.5 * t * (1 - t) + t ** 3
        across = 3 * t * t - 2 * t ** 3
        if td:
            return (p[0] + (q[0] - p[0]) * across,
                    p[1] + (q[1] - p[1]) * along)
        return (p[0] + (q[0] - p[0]) * along, p[1] + (q[1] - p[1]) * across)

    def collides(cx, cy, size):
        lx, ly = cx - size[0] / 2 - 2, cy - size[1] / 2 - 2
        lw, lh = size[0] + 4, size[1] + 4
        for ox, oy, ow, oh in node_obstacles + placed:
            if lx < ox + ow and lx + lw > ox and ly < oy + oh and ly + lh > oy:
                return True
        return False

    def place_label(p, q, size, side=0):
        first = None
        for t in (0.5, 0.4, 0.6, 0.3, 0.7, 0.2, 0.8):
            cx, cy = curve_point(p, q, t)
            if side:
                if td:
                    cx += side * (size[0] / 2 + 5)
                else:
                    cy += side * (size[1] / 2 + 5)
            if first is None:
                first = (cx, cy)
            if not collides(cx, cy, size):
                break
        else:
            cx, cy = first
        placed.append((cx - size[0] / 2, cy - size[1] / 2, size[0], size[1]))
        return (cx, cy)

    same_pair = {}
    for ei, e in edges:
        same_pair.setdefault(frozenset((e.src, e.dst)), []).append(ei)
    pair_side = {}
    for members in same_pair.values():
        members.sort(key=lambda ei: start_u[ei] + end_u[ei])
        for i, ei in enumerate(members):
            centred = i - (len(members) - 1) / 2
            pair_side[ei] = (centred > 0) - (centred < 0)

    paths = []
    for ei, e in edges:
        chain, back = chains[ei]
        virtual = [centre(n) for n in chain[1:-1]]
        pts = ([out_pt(chain[0], start_u[ei])] + virtual
               + [in_pt(chain[-1], end_u[ei])])
        seg = (len(pts) - 2) // 2
        label_pos = None
        if e.label:
            label_pos = place_label(
                pts[seg], pts[seg + 1], label_sizes[ei], pair_side[ei])
        if back:
            pts.reverse()
        paths.append(EdgePath(
            e.src, e.dst, e.label, e.style, e.head, e.tail, pts, label_pos,
            label_sizes.get(ei, (0.0, 0.0)), back))

    # --- normalizar ---
    xs_min, ys_min, xs_max, ys_max = [], [], [], []
    for x, y, w, h in list(rects.values()) + list(grects.values()):
        xs_min.append(x)
        ys_min.append(y)
        xs_max.append(x + w)
        ys_max.append(y + h)
    for path in paths:
        for x, y in path.points:
            xs_min.append(x)
            ys_min.append(y)
            xs_max.append(x)
            ys_max.append(y)
        if path.label_pos:
            lw, lh = path.label_size
            xs_min.append(path.label_pos[0] - lw / 2)
            ys_min.append(path.label_pos[1] - lh / 2)
            xs_max.append(path.label_pos[0] + lw / 2)
            ys_max.append(path.label_pos[1] + lh / 2)
    dx = MARGIN - min(xs_min)
    dy = MARGIN - min(ys_min)
    nodes = {
        nid: NodeBox(nid, graph.nodes[nid].label, graph.nodes[nid].shape,
                     r[0] + dx, r[1] + dy, r[2], r[3])
        for nid, r in rects.items()}
    for path in paths:
        path.points = [(x + dx, y + dy) for x, y in path.points]
        if path.label_pos:
            path.label_pos = (path.label_pos[0] + dx, path.label_pos[1] + dy)
    groups = [
        GroupBox(g.id, g.title, grects[g.id][0] + dx, grects[g.id][1] + dy,
                 grects[g.id][2], grects[g.id][3], g.parent)
        for g in graph.groups if g.id in grects]
    return Layout(
        width=max(xs_max) + dx + MARGIN, height=max(ys_max) + dy + MARGIN,
        direction=direction, nodes=nodes, edges=paths, groups=groups)
