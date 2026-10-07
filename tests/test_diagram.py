import os
os.environ.setdefault("AUDIO_GPT_NO_LOG", "1")
import time
import unittest

import diagram
from fixtures_diagram import (
    ALTW, F1, F2, F3, GARBAGE, SEQUENCE, estimate_measure,
    sol_detail_diagram)


def sample_curve(points, td):
    out = []
    for p, q in zip(points, points[1:]):
        if td:
            d = (q[1] - p[1]) / 2
            c1, c2 = (p[0], p[1] + d), (q[0], q[1] - d)
        else:
            d = (q[0] - p[0]) / 2
            c1, c2 = (p[0] + d, p[1]), (q[0] - d, q[1])
        for i in range(1, 10):
            t = i / 10
            mt = 1 - t
            w = (mt ** 3, 3 * mt * mt * t, 3 * mt * t * t, t ** 3)
            out.append((
                w[0] * p[0] + w[1] * c1[0] + w[2] * c2[0] + w[3] * q[0],
                w[0] * p[1] + w[1] * c1[1] + w[2] * c2[1] + w[3] * q[1]))
    return out


def block(src, fence="```"):
    return f"{fence}mermaid\n{src}\n{fence}"


class SplitTests(unittest.TestCase):
    def test_drops_block_and_collapses_blanks(self):
        md = f"Antes\n\n{block('flowchart TD\n  A-->B')}\n\nDespués"
        out, blocks, open_ = diagram.split_mermaid(md)
        self.assertEqual(out, "Antes\n\nDespués")
        self.assertEqual(blocks, ["flowchart TD\n  A-->B"])
        self.assertFalse(open_)

    def test_no_blocks_unchanged(self):
        md = "texto\n\n```python\nx = 1\n```\n"
        self.assertEqual(diagram.split_mermaid(md), (md, [], False))

    def test_keeps_other_code_blocks(self):
        md = f"```python\nx=1\n```\n\n{block('graph TD\n A-->B')}\n\n```sql\nSELECT 1\n```"
        out, blocks, _ = diagram.split_mermaid(md)
        self.assertIn("```python\nx=1\n```", out)
        self.assertIn("```sql\nSELECT 1\n```", out)
        self.assertNotIn("graph TD", out)
        self.assertEqual(len(blocks), 1)

    def test_rejected_block_stays(self):
        md = f"a\n\n{block(GARBAGE)}\n\n{block('graph TD\n A-->B')}"
        out, blocks, _ = diagram.split_mermaid(
            md, accept=lambda s: diagram.parse_mermaid(s) is not None)
        self.assertEqual(blocks, ["graph TD\n A-->B"])
        self.assertIn("```mermaid\nesto no es mermaid", out)
        self.assertNotIn("A-->B", out)

    def test_trailing_open_block(self):
        md = "Hola\n\n```mermaid\nflowchart TD\n  A --> B"
        out, blocks, open_ = diagram.split_mermaid(md)
        self.assertTrue(open_)
        self.assertEqual(blocks, [])
        self.assertEqual(out, "Hola")

    def test_closed_then_open(self):
        md = f"x\n\n{block('graph TD\n A-->B')}\n\ny\n\n```mermaid\ngraph"
        out, blocks, open_ = diagram.split_mermaid(md)
        self.assertEqual(out, "x\n\ny")
        self.assertEqual(len(blocks), 1)
        self.assertTrue(open_)

    def test_fence_variants(self):
        for fence_open, text in (("~~~ mermaid", "~~~"),
                                 ("```Mermaid  ", "```"),
                                 ("````mermaid", "````")):
            md = f"a\n{fence_open}\ngraph TD\n A-->B\n{text}\nb"
            out, blocks, open_ = diagram.split_mermaid(md)
            self.assertEqual(len(blocks), 1, fence_open)
            self.assertFalse(open_)
            self.assertEqual(out, "a\n\nb")

    def test_longer_fence_ignores_shorter_inner(self):
        md = "````mermaid\ngraph TD\n```\n A-->B\n````\nz"
        out, blocks, _ = diagram.split_mermaid(md)
        self.assertEqual(blocks, ["graph TD\n```\n A-->B"])
        self.assertEqual(out, "z")

    def test_mermaid_inside_other_fence_is_not_a_block(self):
        md = "````text\n```mermaid\ngraph TD\n````\nfin"
        out, blocks, open_ = diagram.split_mermaid(md)
        self.assertEqual((out, blocks, open_), (md, [], False))

    def test_empty(self):
        self.assertEqual(diagram.split_mermaid(""), ("", [], False))
        self.assertEqual(diagram.split_mermaid(None), ("", [], False))


class DropEmptyHeadingsTests(unittest.TestCase):
    def drop(self, md):
        out, _, _ = diagram.split_mermaid(md)
        return diagram.drop_empty_headings(out)

    def test_heading_before_removed_block_then_heading(self):
        md = ("### Números\n\n- uno\n\n### Diagrama\n\n"
              + block("graph TD\n A-->B") + "\n\n### Si te preguntan…\n\n- x")
        out = self.drop(md)
        self.assertNotIn("Diagrama", out)
        self.assertIn("### Números", out)
        self.assertIn("### Si te preguntan…", out)
        self.assertNotIn("\n\n\n", out)

    def test_heading_with_content_stays(self):
        md = "### Detalles\n\n- algo\n\n### Otro\n\ntexto"
        self.assertEqual(diagram.drop_empty_headings(md), md)

    def test_trailing_empty_heading_disappears(self):
        md = "### Detalles\n\n- algo\n\n### Diagrama\n\n"
        self.assertEqual(diagram.drop_empty_headings(md),
                         "### Detalles\n\n- algo")
        self.assertEqual(self.drop(
            "texto\n\n### Diagrama\n\n" + block("graph TD\n A-->B")),
            "texto")

    def test_chain_of_empty_headings(self):
        self.assertEqual(
            diagram.drop_empty_headings("## A\n### B\n\n### C\n"), "")

    def test_code_fences_untouched(self):
        md = "texto\n\n```python\n# comentario\n### no es titulo\n```\n"
        self.assertEqual(diagram.drop_empty_headings(md), md)
        md2 = "### Código\n\n```python\n# comentario\n```"
        self.assertEqual(diagram.drop_empty_headings(md2), md2)

    def test_fenced_hash_line_is_not_a_following_heading(self):
        md = "### Vacío\n\n```\n### dentro\n```"
        self.assertEqual(diagram.drop_empty_headings(md), md)

    def test_empty_inputs(self):
        self.assertEqual(diagram.drop_empty_headings(""), "")
        self.assertIsNone(diagram.drop_empty_headings(None))


class ParseFixtureTests(unittest.TestCase):
    def test_f1(self):
        g = diagram.parse_mermaid(F1)
        self.assertEqual(g.direction, "TD")
        self.assertEqual(list(g.nodes), [
            "user", "cdn", "lb", "api", "cache", "db", "idgen", "q",
            "analytics", "olap"])
        shapes = {k: v.shape for k, v in g.nodes.items()}
        self.assertEqual(shapes["user"], "stadium")
        self.assertEqual(shapes["cdn"], "rect")
        self.assertEqual(shapes["cache"], "db")
        self.assertEqual(shapes["q"], "hexagon")
        self.assertEqual(g.nodes["db"].label, "Postgres shards")
        self.assertEqual(g.nodes["q"].label, "Kafka")
        self.assertEqual(len(g.edges), 10)
        e = g.edges[0]
        self.assertEqual((e.src, e.dst, e.label, e.style, e.head),
                         ("user", "cdn", "GET /abc123", "solid", True))
        dotted = [(e.src, e.dst, e.label) for e in g.edges
                  if e.style == "dotted"]
        self.assertEqual(dotted, [("cache", "db", "miss"),
                                  ("api", "q", "evento click"),
                                  ("q", "analytics", "")])
        self.assertEqual(len(g.groups), 1)
        self.assertEqual(g.groups[0].id, "datos")
        self.assertEqual(g.groups[0].title, "Capa de datos")
        self.assertEqual(set(g.groups[0].members), {"cache", "db"})

    def test_f2(self):
        g = diagram.parse_mermaid(F2)
        self.assertEqual(g.direction, "LR")
        both = [e for e in g.edges if e.src == "rl" and e.dst == "redis"][0]
        self.assertTrue(both.head and both.tail)
        self.assertEqual(both.label, "INCR + TTL")
        self.assertEqual(g.nodes["redis"].shape, "db")
        self.assertEqual(g.nodes["client"].shape, "stadium")
        back = [e for e in g.edges if e.dst == "client"][0]
        self.assertEqual((back.src, back.label), ("rl", "429"))
        self.assertEqual(g.edges[-1].style, "dotted")
        self.assertFalse(g.edges[-1].tail)

    def test_f3(self):
        g = diagram.parse_mermaid(F3)
        self.assertEqual(g.nodes["K"].label, "Kafka: posts")
        self.assertEqual(g.nodes["K"].shape, "hexagon")
        self.assertEqual(g.nodes["T"].shape, "db")
        self.assertEqual(len(g.edges), 9)
        self.assertEqual(
            [e.label for e in g.edges if e.label],
            ["nuevo post", "push ids", "celebrities: pull"])

    def test_sol_detail_block(self):
        src = sol_detail_diagram()
        if src is None:
            self.skipTest("sol_detail.md no disponible")
        g = diagram.parse_mermaid(src)
        self.assertIsNotNone(g)
        self.assertGreaterEqual(len(g.nodes), 3)
        self.assertGreaterEqual(len(g.edges), 2)

    def test_non_flowchart_and_garbage(self):
        self.assertIsNone(diagram.parse_mermaid(SEQUENCE))
        self.assertIsNone(diagram.parse_mermaid(GARBAGE))
        for head in ("erDiagram", "classDiagram", "stateDiagram-v2",
                     "gantt", "pie title X", "mindmap", "journey"):
            self.assertIsNone(diagram.parse_mermaid(head + "\n  A --> B"))
        self.assertIsNone(diagram.parse_mermaid(""))
        self.assertIsNone(diagram.parse_mermaid("flowchart TD"))
        self.assertIsNone(diagram.parse_mermaid(None))


class ParseSyntaxTests(unittest.TestCase):
    def parse(self, body, head="flowchart TD"):
        return diagram.parse_mermaid(head + "\n" + body)

    def test_directions(self):
        for head, expected in (("graph", "TD"), ("flowchart TB", "TD"),
                               ("flowchart BT", "TD"), ("graph LR", "LR"),
                               ("graph RL", "LR"), ("flowchart TD", "TD")):
            g = diagram.parse_mermaid(head + "\nA-->B")
            self.assertEqual(g.direction, expected, head)

    def test_all_shapes(self):
        g = self.parse(
            'a(((c1)))\nb((c2))\nc([st])\nd(rd)\ne[[sub]]\nf[(db)]\n'
            'g[/pa/]\nh[\\pb\\]\ni[rc]\nj{{hx}}\nk{dm}\nl>as]')
        got = {k: (v.shape, v.label) for k, v in g.nodes.items()}
        self.assertEqual(got, {
            "a": ("circle", "c1"), "b": ("circle", "c2"),
            "c": ("stadium", "st"), "d": ("round", "rd"),
            "e": ("subroutine", "sub"), "f": ("db", "db"),
            "g": ("para", "pa"), "h": ("para", "pb"),
            "i": ("rect", "rc"), "j": ("hexagon", "hx"),
            "k": ("diamond", "dm"), "l": ("asym", "as")})

    def test_quoted_labels_with_brackets(self):
        g = self.parse('A["List[int] (x)"] --> B("a <br/> b")')
        self.assertEqual(g.nodes["A"].label, "List[int] (x)")
        self.assertEqual(g.nodes["B"].label, "a\nb")

    def test_label_cleanup(self):
        g = self.parse("A[`**Bold** one<br>two`]")
        self.assertEqual(g.nodes["A"].label, "Bold one\ntwo")

    def test_bare_node_label_and_late_shape(self):
        g = self.parse("A --> B\nB[(Store)]\nC")
        self.assertEqual(g.nodes["A"].label, "A")
        self.assertEqual(g.nodes["B"].shape, "db")
        self.assertEqual(g.nodes["B"].label, "Store")
        self.assertIn("C", g.nodes)

    def test_edge_kinds(self):
        g = self.parse(
            "A --> B\nB --- C\nC -.-> D\nD -.- E\nE ==> F\nF === G\n"
            "G <--> H\nH --o I\nI --x J\nJ --->K")
        got = [(e.style, e.head, e.tail) for e in g.edges]
        self.assertEqual(got, [
            ("solid", True, False), ("solid", False, False),
            ("dotted", True, False), ("dotted", False, False),
            ("thick", True, False), ("thick", False, False),
            ("solid", True, True), ("solid", True, False),
            ("solid", True, False), ("solid", True, False)])

    def test_inline_text_forms(self):
        g = self.parse(
            "A -- uno --> B\nB -. dos .-> C\nC == tres ==> D\nD -- cuatro --- E")
        got = [(e.src, e.dst, e.label, e.style, e.head) for e in g.edges]
        self.assertEqual(got, [
            ("A", "B", "uno", "solid", True),
            ("B", "C", "dos", "dotted", True),
            ("C", "D", "tres", "thick", True),
            ("D", "E", "cuatro", "solid", False)])

    def test_pipe_label_without_spaces(self):
        g = self.parse("A-->|x y|B")
        self.assertEqual((g.edges[0].label, g.edges[0].dst), ("x y", "B"))

    def test_chains_and_fanout(self):
        g = self.parse("A --> B --> C\nX & Y --> Z & W")
        pairs = [(e.src, e.dst) for e in g.edges]
        self.assertEqual(pairs, [
            ("A", "B"), ("B", "C"), ("X", "Z"), ("X", "W"), ("Y", "Z"),
            ("Y", "W")])

    def test_semicolons_and_comments(self):
        g = diagram.parse_mermaid(
            "graph TD; A-->B; %% nada\n%% otro\nB-->C;")
        self.assertEqual([(e.src, e.dst) for e in g.edges],
                         [("A", "B"), ("B", "C")])

    def test_ignored_statements(self):
        g = self.parse(
            "classDef hot fill:#f00\nA:::hot --> B\nclass A,B hot\n"
            "style A fill:#fff\nlinkStyle 0 stroke:red\nclick A cb\n"
            "direction LR\naccTitle: t\naccDescr: d\nB --> C")
        self.assertEqual(list(g.nodes), ["A", "B", "C"])
        self.assertEqual(len(g.edges), 2)

    def test_hyphenated_ids(self):
        g = self.parse("api-gw --> user-db\nx-->y")
        self.assertEqual(list(g.nodes), ["api-gw", "user-db", "x", "y"])

    def test_bad_statement_skipped(self):
        g = self.parse("A --> B\n??? ((( broken\nB --> C")
        self.assertEqual(len(g.edges), 2)

    def test_subgraph_forms(self):
        g = self.parse(
            "subgraph one [Uno]\n a\nend\n"
            "subgraph two[Dos]\n b\nend\n"
            'subgraph "Tres tres"\n c\nend\n'
            "subgraph Cuatro cuatro\n d\nend")
        self.assertEqual([(x.id, x.title) for x in g.groups], [
            ("one", "Uno"), ("two", "Dos"), ("Tres_tres", "Tres tres"),
            ("Cuatro_cuatro", "Cuatro cuatro")])
        self.assertEqual([x.members for x in g.groups],
                         [["a"], ["b"], ["c"], ["d"]])

    def test_nested_subgraphs_first_mention(self):
        g = self.parse(
            "x --> y\nsubgraph outer\n o1\n subgraph inner[In]\n  i1\n"
            "  y --> i2\n end\n o2\nend")
        groups = {x.id: x for x in g.groups}
        self.assertEqual(groups["inner"].parent, "outer")
        self.assertEqual(groups["inner"].members, ["i1", "i2"])
        self.assertEqual(groups["outer"].members, ["o1", "o2"])


class LayoutTests(unittest.TestCase):
    def layout(self, source, direction=None):
        g = diagram.parse_mermaid(source)
        return g, diagram.layout_graph(
            g, estimate_measure, direction or g.direction)

    def assertNoOverlap(self, lay):
        boxes = list(lay.nodes.values())
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                overlap = (a.x < b.x + b.w and b.x < a.x + a.w
                           and a.y < b.y + b.h and b.y < a.y + a.h)
                self.assertFalse(overlap, (a.id, b.id))

    def test_fixtures_basic_invariants(self):
        for src in (F1, F2, F3):
            for direction in ("TD", "LR"):
                g, lay = self.layout(src, direction)
                self.assertEqual(set(lay.nodes), set(g.nodes))
                self.assertNoOverlap(lay)
                self.assertGreater(lay.width, 0)
                self.assertGreater(lay.height, 0)
                for box in lay.nodes.values():
                    self.assertGreaterEqual(box.x, diagram.MARGIN - 0.01)
                    self.assertGreaterEqual(box.y, diagram.MARGIN - 0.01)
                    self.assertLessEqual(box.x + box.w, lay.width)
                    self.assertLessEqual(box.y + box.h, lay.height)

    def test_td_forward_edges_go_down(self):
        for src in (F1, F3):
            g, lay = self.layout(src, "TD")
            for path in lay.edges:
                if path.back:
                    continue
                ys = [p[1] for p in path.points]
                self.assertEqual(ys, sorted(ys), (path.src, path.dst))
                self.assertLess(ys[0], ys[-1])
                src_box, dst_box = lay.nodes[path.src], lay.nodes[path.dst]
                self.assertLess(src_box.y, dst_box.y)

    def test_lr_forward_edges_go_right(self):
        g, lay = self.layout(F2, "LR")
        for path in lay.edges:
            if path.back:
                continue
            xs = [p[0] for p in path.points]
            self.assertEqual(xs, sorted(xs))
            self.assertLess(lay.nodes[path.src].x, lay.nodes[path.dst].x)

    def test_f2_back_edge(self):
        g, lay = self.layout(F2)
        backs = [p for p in lay.edges if p.back]
        self.assertEqual([(p.src, p.dst) for p in backs], [("rl", "client")])
        path = backs[0]
        self.assertGreaterEqual(len(path.points), 3)
        xs = [p[0] for p in path.points]
        self.assertEqual(xs, sorted(xs, reverse=True))
        client, rl = lay.nodes["client"], lay.nodes["rl"]
        self.assertAlmostEqual(path.points[-1][0], client.x + client.w)
        self.assertAlmostEqual(path.points[0][0], rl.x)

    def test_back_edge_td_goes_up_to_dst(self):
        g, lay = self.layout(ALTW, "TD")
        path = [p for p in lay.edges if (p.src, p.dst) == ("db", "cache")][0]
        self.assertTrue(path.back)
        cache = lay.nodes["cache"]
        self.assertAlmostEqual(path.points[-1][1], cache.y + cache.h)
        ys = [p[1] for p in path.points]
        self.assertEqual(ys, sorted(ys, reverse=True))

    def test_parallel_edges_use_distinct_ports(self):
        g, lay = self.layout(ALTW, "TD")
        fwd = [p for p in lay.edges if (p.src, p.dst) == ("cache", "db")][0]
        back = [p for p in lay.edges if (p.src, p.dst) == ("db", "cache")][0]
        at_cache = fwd.points[0][0] - back.points[-1][0]
        at_db = fwd.points[-1][0] - back.points[0][0]
        self.assertGreaterEqual(abs(at_cache), 8)
        self.assertGreaterEqual(abs(at_db), 8)
        self.assertGreater(at_cache * at_db, 0)
        cache, db = lay.nodes["cache"], lay.nodes["db"]
        for x in (fwd.points[0][0], back.points[-1][0]):
            self.assertGreaterEqual(x, cache.x + 0.2 * cache.w - 0.01)
            self.assertLessEqual(x, cache.x + 0.8 * cache.w + 0.01)
        for x in (fwd.points[-1][0], back.points[0][0]):
            self.assertGreaterEqual(x, db.x + 0.2 * db.w - 0.01)
            self.assertLessEqual(x, db.x + 0.8 * db.w + 0.01)
        solo = [p for p in lay.edges if (p.src, p.dst) == ("lb", "app")][0]
        lb = lay.nodes["lb"]
        self.assertAlmostEqual(solo.points[0][0], lb.cx)

    def test_f1_edges_into_db_end_apart(self):
        g, lay = self.layout(F1, "TD")
        ends = [p.points[-1] for p in lay.edges if p.dst == "db"]
        self.assertEqual(len(ends), 2)
        self.assertGreaterEqual(abs(ends[0][0] - ends[1][0]), 8)
        db = lay.nodes["db"]
        for x, y in ends:
            self.assertAlmostEqual(y, db.y)
            self.assertTrue(db.x < x < db.x + db.w)

    def test_ports_follow_neighbour_order_in_lr(self):
        g = diagram.parse_mermaid("graph LR\nA --> C\nB --> C\nA --> D")
        lay = diagram.layout_graph(g, estimate_measure, "LR")
        c = lay.nodes["C"]
        ends = sorted((p.points[-1][1], p.src) for p in lay.edges
                      if p.dst == "C")
        by_source = [src for _, src in sorted(
            (lay.nodes[x].cy, x) for x in ("A", "B"))]
        self.assertEqual([src for _, src in ends], by_source)
        for y, _ in ends:
            self.assertTrue(c.y < y < c.y + c.h)
        self.assertGreaterEqual(ends[1][0] - ends[0][0], 8)

    def test_edge_curves_avoid_foreign_nodes(self):
        sources = [F1, F2, F3, ALTW]
        extra = sol_detail_diagram()
        if extra:
            sources.append(extra)
        for src in sources:
            g = diagram.parse_mermaid(src)
            for direction in ("TD", "LR"):
                lay = diagram.layout_graph(g, estimate_measure, direction)
                for path in lay.edges:
                    others = [b for nid, b in lay.nodes.items()
                              if nid not in (path.src, path.dst)]
                    for x, y in sample_curve(path.points, direction == "TD"):
                        for b in others:
                            inside = (b.x + 3 < x < b.x + b.w - 3
                                      and b.y + 3 < y < b.y + b.h - 3)
                            self.assertFalse(
                                inside,
                                (direction, path.src, path.dst, b.id))

    def test_groups_contain_members(self):
        for src in (F1,):
            for direction in ("TD", "LR"):
                g, lay = self.layout(src, direction)
                box = [b for b in lay.groups if b.id == "datos"][0]
                for member in ("cache", "db"):
                    n = lay.nodes[member]
                    self.assertGreaterEqual(n.x, box.x)
                    self.assertGreaterEqual(n.y, box.y + 16)
                    self.assertLessEqual(n.x + n.w, box.x + box.w)
                    self.assertLessEqual(n.y + n.h, box.y + box.h)
                for nid, n in lay.nodes.items():
                    if nid in ("cache", "db"):
                        continue
                    inside = (box.x < n.cx < box.x + box.w
                              and box.y < n.cy < box.y + box.h)
                    self.assertFalse(inside, (direction, nid))

    def test_nested_group_boxes(self):
        g = diagram.parse_mermaid(
            "flowchart TD\nsubgraph outer\n a --> b\n subgraph inner\n  c\n"
            " end\nend\nb --> c\nz --> a")
        lay = diagram.layout_graph(g, estimate_measure)
        boxes = {b.id: b for b in lay.groups}
        o, i = boxes["outer"], boxes["inner"]
        self.assertLessEqual(o.x, i.x)
        self.assertLessEqual(o.y, i.y)
        self.assertGreaterEqual(o.x + o.w, i.x + i.w)
        self.assertGreaterEqual(o.y + o.h, i.y + i.h)
        z = lay.nodes["z"]
        self.assertFalse(o.x < z.cx < o.x + o.w and o.y < z.cy < o.y + o.h)

    def test_cycles_do_not_crash(self):
        for body in ("A --> B --> C --> A", "A --> A", "A <--> B",
                     "A --> B\nB --> A\nB --> C\nC --> B"):
            g = diagram.parse_mermaid("graph TD\n" + body)
            lay = diagram.layout_graph(g, estimate_measure)
            self.assertEqual(set(lay.nodes), set(g.nodes))
            self.assertNoOverlap(lay)

    def test_single_and_isolated_nodes(self):
        g = diagram.parse_mermaid("graph TD\nA\nB\nC --> D")
        lay = diagram.layout_graph(g, estimate_measure)
        self.assertEqual(len(lay.nodes), 4)
        self.assertNoOverlap(lay)

    def test_long_edge_gets_virtual_points(self):
        g = diagram.parse_mermaid("graph TD\nA-->B-->C-->D\nA-->D")
        lay = diagram.layout_graph(g, estimate_measure)
        long_edge = [p for p in lay.edges if (p.src, p.dst) == ("A", "D")][0]
        self.assertEqual(len(long_edge.points), 4)

    def test_deterministic(self):
        for src in (F1, F2, F3):
            g = diagram.parse_mermaid(src)
            a = diagram.layout_graph(g, estimate_measure, g.direction)
            b = diagram.layout_graph(g, estimate_measure, g.direction)
            self.assertEqual(a, b)

    def test_shape_sizes(self):
        g = diagram.parse_mermaid(
            "graph TD\na[rc]\nb[(db)]\nc{dm}\nd((ci))\ne{{hx}}\nf([st])")
        lay = diagram.layout_graph(g, estimate_measure)
        n = lay.nodes
        self.assertGreaterEqual(n["a"].w, 64)
        self.assertGreaterEqual(n["a"].h, 30)
        self.assertEqual(n["b"].h, n["a"].h + 10)
        self.assertAlmostEqual(n["c"].w, n["a"].w * 1.35)
        self.assertAlmostEqual(n["c"].h, n["a"].h * 1.5)
        self.assertEqual(n["d"].w, n["d"].h)
        self.assertEqual(n["e"].w, n["a"].w + 16)
        self.assertEqual(n["f"].w, n["a"].w + 12)

    def test_edge_label_positions(self):
        g, lay = self.layout(F1)
        for path in lay.edges:
            if path.label:
                self.assertIsNotNone(path.label_pos)
                self.assertGreater(path.label_size[0], 0)
            else:
                self.assertIsNone(path.label_pos)

    def test_large_graph_is_fast(self):
        lines = ["flowchart TD"]
        for i in range(30):
            lines.append(f"n{i}[Nodo {i}] --> n{(i * 7 + 3) % 30}")
            lines.append(f"n{i} --> n{(i + 1) % 30}")
            if i % 3 == 0:
                lines.append(f"n{i} -->|x| n{(i + 5) % 30}")
        g = diagram.parse_mermaid("\n".join(lines))
        self.assertEqual(len(g.nodes), 30)
        start = time.perf_counter()
        lay = diagram.layout_graph(g, estimate_measure)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.3)
        self.assertEqual(len(lay.nodes), 30)

    def test_lr_label_widens_rank_gap(self):
        g1 = diagram.parse_mermaid("graph LR\nA --> B")
        g2 = diagram.parse_mermaid(
            "graph LR\nA -->|una etiqueta bastante larga de verdad| B")
        l1 = diagram.layout_graph(g1, estimate_measure, "LR")
        l2 = diagram.layout_graph(g2, estimate_measure, "LR")
        self.assertGreater(l2.width, l1.width)


if __name__ == "__main__":
    unittest.main()
