import os
os.environ.setdefault("AUDIO_GPT_NO_LOG", "1")
import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

import gui
from fixtures_diagram import F1, F2, F3, GARBAGE

APP = QApplication.instance() or QApplication([])

LUNA = "**Di ahora:** Usaría una caché delante de la base.\n\n- Redis\n- CDN"
SOL = "### Detalles\n\n- Réplicas de lectura\n\n```python\nx = 1\n```"


def pump(ms=120):
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        APP.processEvents()
        time.sleep(0.005)


class AnswerFeedDetailTests(unittest.TestCase):
    def setUp(self):
        self.feed = gui.AnswerFeed(font_px=13)
        self.feed.resize(460, 700)
        self.feed.show()
        self.root = object()
        self.detail = object()
        card = self.feed.start(self.root, "Manual", "gpt-6-luna", "¿cómo?")
        self.card = card
        pump()

    def tearDown(self):
        self.feed.close()

    def finish_fast(self):
        self.feed.update(self.root, LUNA[:20])
        self.feed.update(self.root, LUNA)
        self.feed.finish(self.root, True, LUNA)
        pump()

    def test_full_flow(self):
        self.finish_fast()
        self.assertTrue(self.card.detail.isHidden())
        self.feed.start_detail(self.root, self.detail, "gpt-6.1-sol",
                               "en cola tras gpt-6-luna…")
        pump()
        self.assertFalse(self.card.detail.isHidden())
        self.assertEqual(self.card.detail.header.full_text(),
                         "gpt-6.1-sol · detalles · en cola tras gpt-6-luna…")
        self.assertTrue(self.card.detail.body.isHidden())

        self.feed.begin_detail(self.detail)
        pump(250)
        self.assertIn("pensando…", self.card.detail.header.full_text())

        self.feed.update(self.detail, "### Detalles\n\n- uno")
        pump()
        self.assertFalse(self.card.detail.body.isHidden())
        self.feed.finish(self.detail, True, SOL)
        pump()
        self.assertIn("Réplicas", self.card.detail.body.toPlainText())
        self.assertTrue(self.card.detail.header.full_text().startswith(
            "gpt-6.1-sol · detalles · "))
        self.assertTrue(self.card.detail.header.full_text().endswith("s"))

        main_top = self.card.body.mapTo(self.card, QPoint(0, 0)).y()
        detail_top = self.card.detail.mapTo(self.card, QPoint(0, 0)).y()
        self.assertGreaterEqual(
            detail_top, main_top + self.card.body.height())
        self.assertIn("Redis", self.card.body.toPlainText())
        self.assertTrue(self.card.has_code_block())
        self.assertEqual(self.card.longest_code_line(), len("x = 1"))

    def test_metrics_stick_in_header(self):
        self.finish_fast()
        self.feed.status(self.root, "1.4s · 1er token 0.6s · caché 97%")
        pump(250)
        self.assertEqual(
            self.card.header_label.full_text(),
            "gpt-6-luna · 1.4s · 1er token 0.6s · caché 97%")

        self.feed.start_detail(self.root, self.detail, "gpt-6.1-sol", "cola")
        self.feed.begin_detail(self.detail)
        self.feed.update(self.detail, SOL)
        self.feed.finish(self.detail, True, SOL)
        self.feed.status(self.detail, "5.0s · caché 90%")
        pump(250)
        self.assertEqual(self.card.detail.header.full_text(),
                         "gpt-6.1-sol · detalles · 5.0s · caché 90%")

    def test_detail_error(self):
        self.finish_fast()
        self.feed.start_detail(self.root, self.detail, "gpt-6.1-sol", "cola")
        self.feed.finish(self.detail, False, "Error: timeout")
        pump()
        self.assertEqual(self.card.detail.header.full_text(),
                         "gpt-6.1-sol · detalles · error: timeout")
        self.assertIn("#e57373", self.card.detail.header.styleSheet())

    def test_followup_hides_detail(self):
        self.finish_fast()
        self.feed.start_detail(self.root, self.detail, "gpt-6.1-sol", "cola")
        self.feed.finish(self.detail, True, SOL)
        pump()
        self.assertFalse(self.card.detail.isHidden())
        follow = object()
        self.feed.start_followup(self.root, follow, "gpt-6.1-sol · web")
        pump()
        self.assertFalse(self.card.detail.isHidden())
        self.feed.update(follow, "Versión mejorada")
        self.feed.finish(follow, True, "Versión mejorada")
        pump()
        self.assertTrue(self.card.detail.isHidden())
        self.assertEqual(self.card.body.toPlainText().strip(),
                         "Versión mejorada")

    def test_cancel_detail_hides(self):
        self.finish_fast()
        self.feed.start_detail(self.root, self.detail, "gpt-6.1-sol", "cola")
        pump()
        self.assertFalse(self.card.detail.isHidden())
        self.feed.cancel_detail(self.detail)
        pump()
        self.assertTrue(self.card.detail.isHidden())
        self.feed.update(self.detail, "tarde")
        self.assertEqual(self.card.detail.text, "")

    def test_timer_runs_while_detail_pending(self):
        self.finish_fast()
        pump(150)
        self.assertFalse(self.card._elapsed_timer.isActive())
        self.feed.start_detail(self.root, self.detail, "gpt-6.1-sol", "cola")
        self.assertTrue(self.card._elapsed_timer.isActive())
        self.feed.finish(self.detail, True, SOL)
        pump(250)
        self.assertFalse(self.card._elapsed_timer.isActive())


class AnswerFeedDiagramTests(unittest.TestCase):
    def setUp(self):
        self.feed = gui.AnswerFeed(font_px=13)
        self.feed.resize(460, 900)
        self.feed.show()
        self.root = object()
        self.detail = object()
        self.card = self.feed.start(self.root, "Manual", "gpt-6-luna", "¿?")
        pump()

    def tearDown(self):
        self.feed.close()

    def version_stack(self):
        return self.card.current_version.stack

    def test_open_fence_shows_placeholder_only(self):
        text = "Texto previo\n\n```mermaid\nflowchart TD\n  A --> B"
        self.feed.update(self.root, text)
        pump(150)
        stack = self.version_stack()
        self.assertFalse(stack.placeholder.isHidden())
        self.assertEqual(len(stack.views), 0)
        self.assertNotIn("flowchart", self.card.body.toPlainText())
        self.assertIn("Texto previo", self.card.body.toPlainText())

    def test_closed_block_becomes_one_view(self):
        text = f"Texto previo\n\n```mermaid\n{F1}\n```\n\nPie"
        self.feed.update(self.root, text[:40])
        self.feed.update(self.root, text)
        self.feed.finish(self.root, True, text)
        pump(150)
        stack = self.version_stack()
        self.assertEqual(len(stack.views), 1)
        self.assertTrue(stack.placeholder.isHidden())
        body = self.card.body.toPlainText()
        self.assertNotIn("flowchart", body)
        self.assertIn("Pie", body)
        self.assertGreater(self.card.diagram_width(), 0)
        self.assertGreater(self.feed.diagram_width(), 0)
        self.assertFalse(self.card.has_code_block())
        self.assertEqual(self.card.longest_code_line(), 0)
        main_top = self.card.body.mapTo(self.card, QPoint(0, 0)).y()
        stack_top = stack.mapTo(self.card, QPoint(0, 0)).y()
        self.assertGreaterEqual(stack_top, main_top + self.card.body.height())

    def test_diagram_only_answer_hides_empty_body(self):
        text = f"```mermaid\n{F2}\n```"
        self.feed.finish(self.root, True, text)
        pump(150)
        self.assertEqual(len(self.version_stack().views), 1)
        self.assertTrue(self.card.body.isHidden())

    def test_invalid_block_stays_as_code(self):
        text = f"Antes\n\n```mermaid\n{GARBAGE}\n```"
        self.feed.finish(self.root, True, text)
        pump(150)
        self.assertEqual(len(self.version_stack().views), 0)
        self.assertIn("esto no es mermaid", self.card.body.toPlainText())
        self.assertTrue(self.card.has_code_block())

    def test_detail_renders_diagrams(self):
        self.feed.finish(self.root, True, LUNA)
        self.feed.start_detail(self.root, self.detail, "gpt-6.1-sol", "cola")
        self.feed.begin_detail(self.detail)
        text = f"### Diagrama\n\n```mermaid\n{F3}\n```"
        self.feed.update(self.detail, text[:20])
        self.feed.update(self.detail, text)
        self.feed.finish(self.detail, True, text)
        pump(150)
        stack = self.card.detail.stack
        self.assertEqual(len(stack.views), 1)
        self.assertNotIn("flowchart", self.card.detail.body.toPlainText())
        self.assertNotIn("Diagrama", self.card.detail.body.toPlainText())
        self.assertEqual(stack.caption.text(), "Diagrama")
        self.assertFalse(stack.caption.isHidden())
        self.assertGreater(self.card.diagram_width(), 0)
        detail_top = stack.mapTo(self.card, QPoint(0, 0)).y()
        main_top = self.card.body.mapTo(self.card, QPoint(0, 0)).y()
        self.assertGreater(detail_top, main_top + self.card.body.height())

    def test_detail_open_fence_placeholder(self):
        self.feed.finish(self.root, True, LUNA)
        self.feed.start_detail(self.root, self.detail, "gpt-6.1-sol", "cola")
        self.feed.update(self.detail, "### D\n\n```mermaid\nflowchart TD\n A")
        pump(150)
        self.assertFalse(self.card.detail.stack.placeholder.isHidden())
        self.assertNotIn("flowchart", self.card.detail.body.toPlainText())

    def test_history_expand_collapse_with_diagram(self):
        text = f"Primera\n\n```mermaid\n{F1}\n```"
        self.feed.finish(self.root, True, text)
        pump()
        follow = object()
        self.feed.start_followup(self.root, follow, "gpt-6.1-sol")
        self.feed.update(follow, "Versión nueva")
        self.feed.finish(follow, True, "Versión nueva")
        pump(150)
        first = self.card.versions[0]
        self.assertTrue(first.content.isHidden())
        self.card.toggle_version(self.root)
        pump(100)
        self.assertFalse(first.content.isHidden())
        self.assertEqual(len(first.stack.views), 1)
        self.assertGreater(self.card.diagram_width(), 0)
        self.card.toggle_version(self.root)
        pump(100)
        self.assertTrue(first.content.isHidden())
        self.assertEqual(self.card.body.toPlainText().strip(), "Versión nueva")


if __name__ == "__main__":
    unittest.main()
