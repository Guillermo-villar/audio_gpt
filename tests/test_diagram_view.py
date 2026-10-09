import os
os.environ.setdefault("AUDIO_GPT_NO_LOG", "1")
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

import diagram
import diagram_view
from fixtures_diagram import F1, F2, F3, sol_detail_diagram

APP = QApplication.instance() or QApplication([])

CHAIN = "graph TD\n" + " --> ".join(f"n{i}[Paso {i}]" for i in range(6))


def render(source, width):
    graph = diagram.parse_mermaid(source)
    view = diagram_view.DiagramView(graph)
    view.resize(width, 300)
    view.show()
    APP.processEvents()
    image = QImage(view.size(), QImage.Format_ARGB32)
    image.fill(QColor("#000000"))
    view.render(image)
    return view, image


class DiagramViewTests(unittest.TestCase):
    def test_fixtures_paint(self):
        for name, src in (("F1", F1), ("F2", F2), ("F3", F3)):
            for width in (440, 760):
                view, image = render(src, width)
                self.assertGreater(view.height(), 0, (name, width))
                self.assertEqual(image.width(), width)
                self.assertEqual(image.height(), view.height())
                self.assertNotEqual(image.pixelColor(width // 2, 1),
                                    QColor("#000000"))
                view.close()

    def test_sol_detail_diagram_paints(self):
        src = sol_detail_diagram()
        if src is None:
            self.skipTest("sol_detail.md no disponible")
        view, image = render(src, 440)
        self.assertGreater(view.height(), 0)
        view.close()

    def test_linear_chain_chooses_td(self):
        view, _ = render(CHAIN, 440)
        self.assertEqual(view.direction, "TD")
        self.assertEqual(view.scale, 1.0)
        view.close()

    def test_wide_td_declared_picks_lr_when_much_better(self):
        graph = diagram.parse_mermaid(
            "graph TD\n" + "\n".join(f"a --> b{i}" for i in range(8)))
        view = diagram_view.DiagramView(graph)
        view.resize(440, 100)
        td, lr = view._layouts["TD"], view._layouts["LR"]
        self.assertGreater(td.width, lr.width)
        self.assertEqual(view.direction, "LR")
        self.assertGreaterEqual(view.scale, 0.9)

    def test_height_follows_width_and_signal(self):
        graph = diagram.parse_mermaid(F1)
        view = diagram_view.DiagramView(graph)
        hits = []
        view.height_changed.connect(lambda: hits.append(view.height()))
        view.show()
        view.resize(760, 100)
        APP.processEvents()
        h_wide = view.height()
        view.resize(300, 100)
        APP.processEvents()
        self.assertGreater(len(hits), 0)
        view.close()
        self.assertNotEqual(view.height(), h_wide)
        self.assertGreater(view.natural_width(), 100)

    def test_natural_width_uses_td(self):
        view = diagram_view.DiagramView(diagram.parse_mermaid(F1))
        self.assertAlmostEqual(
            view.natural_width(),
            view._layouts["TD"].width + 2 * diagram_view.VIEW_MARGIN)

    def test_roles(self):
        role = diagram_view.node_role
        self.assertEqual(role("Redis", "cache", "db"), "cache")
        self.assertEqual(role("Timeline cache", "T", "db"), "cache")
        self.assertEqual(role("Kafka", "q", "hexagon"), "queue")
        self.assertEqual(role("Workers", "w", "hexagon"), "queue")
        self.assertEqual(role("Object storage", "o", "rect"), "storage")
        self.assertEqual(role("S3 bucket", "x", "rect"), "storage")
        self.assertEqual(role("Postgres shards", "db", "db"), "db")
        self.assertEqual(role("Posts DB", "P", "db"), "db")
        self.assertEqual(role("Store", "s", "db"), "db")
        self.assertEqual(role("Usuario", "user", "stadium"), "client")
        self.assertEqual(role("Algo", "a", "stadium"), "client")
        self.assertEqual(role("Load Balancer", "lb", "rect"), "infra")
        self.assertEqual(role("API Gateway", "gw", "rect"), "infra")
        self.assertEqual(role("CDN", "cdn", "rect"), "infra")
        self.assertEqual(role("API stateless", "api", "rect"), "default")
        self.assertEqual(role("Handler", "h", "rect"), "default")
        self.assertEqual(role("Padding", "p", "rect"), "default")
        self.assertEqual(role("Dbus thing", "t", "rect"), "default")
        self.assertEqual(role("MySQL", "m", "rect"), "db")
        self.assertEqual(role("user-db", "x", "rect"), "db")

    def test_stack(self):
        stack = diagram_view.DiagramStack()
        graphs = {F1: diagram.parse_mermaid(F1)}
        stack.set_sources({}, False)
        self.assertTrue(stack.isHidden())
        stack.set_sources({}, True)
        self.assertFalse(stack.isHidden())
        self.assertFalse(stack.placeholder.isHidden())
        stack.set_sources(graphs, False)
        self.assertEqual(len(stack.views), 1)
        self.assertTrue(stack.placeholder.isHidden())
        first = stack.views[0]
        stack.set_sources(graphs, False)
        self.assertIs(stack.views[0], first)
        graphs[F2] = diagram.parse_mermaid(F2)
        stack.set_sources(graphs, True)
        self.assertEqual(len(stack.views), 2)
        self.assertGreater(stack.natural_width(), 0)
        stack.set_sources({}, False)
        self.assertEqual(stack.views, [])
        self.assertTrue(stack.isHidden())


if __name__ == "__main__":
    unittest.main()
