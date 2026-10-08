import os
os.environ.setdefault("AUDIO_GPT_NO_LOG", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time
import unittest

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

import gui
from fixtures_diagram import F1, F2

APP = QApplication.instance() or QApplication([])

LUNA = "**Di ahora:** Usaría una caché delante de la base.\n\n- Redis\n- CDN"
SOL = "### Detalles\n\n- Réplicas de lectura\n\n- Particionar por hash"
LONG = "\n\n".join(f"- punto largo número {i} con algo de texto" for i in range(60))


def pump(ms=150):
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        APP.processEvents()
        time.sleep(0.005)


class SideBySideFeedTests(unittest.TestCase):
    def setUp(self):
        self.feed = gui.AnswerFeed(font_px=13, side_by_side=True)
        self.feed.resize(1000, 700)
        self.feed.show()
        self.root = object()
        self.detail = object()
        self.card = self.feed.start(self.root, "Manual", "gpt-6-luna", "¿?")
        pump()

    def tearDown(self):
        self.feed.close()

    def finish_luna(self):
        self.feed.update(self.root, LUNA)
        self.feed.finish(self.root, True, LUNA)
        self.feed.status(self.root, "1.4s · 1er token 0.6s · caché 97%")
        pump()

    def run_detail(self, text=SOL):
        self.feed.start_detail(self.root, self.detail, "gpt-6.1-sol", "cola")
        self.feed.begin_detail(self.detail)
        self.feed.update(self.detail, text)
        self.feed.finish(self.detail, True, text)
        self.feed.status(self.detail, "5.0s · caché 90%")
        pump(250)

    def test_luna_left_and_detail_right(self):
        self.finish_luna()
        self.run_detail()
        card = self.card
        self.assertTrue(card.two_columns)
        root = card.versions[0]
        self.assertTrue(card.left_panel.isAncestorOf(root.body))
        self.assertIn("Di ahora", root.body.toPlainText())
        self.assertTrue(card.right_panel.isAncestorOf(card.detail.body))
        self.assertIn("Réplicas", card.detail.body.toPlainText())
        left_right_edge = card.left_scroll.geometry().right()
        right_x = card.right_scroll.mapTo(card, QPoint(0, 0)).x()
        self.assertGreaterEqual(right_x, left_right_edge)
        self.assertTrue(card.separator.isVisible())
        self.assertIn("caché 97%", card.header_label.full_text())
        self.assertIn("caché 90%", card.detail.header.full_text())
        self.assertTrue(card.right_header_label.isHidden())

    def test_content_height_is_taller_column(self):
        self.finish_luna()
        self.run_detail(SOL + "\n\n" + LONG)
        card = self.card
        left = card._column_height(card.left_scroll, card.left_panel)
        right = card._column_height(card.right_scroll, card.right_panel)
        self.assertGreater(right, left)
        self.assertEqual(card.content_height(), right + 6)
        self.assertEqual(self.feed.content_height(), card.content_height())

    def test_single_column_without_detail(self):
        self.finish_luna()
        self.assertFalse(self.card.two_columns)
        self.assertTrue(self.card.right_scroll.isHidden())
        self.assertTrue(self.card.separator.isHidden())
        left = self.card._column_height(
            self.card.left_scroll, self.card.left_panel)
        self.assertEqual(self.card.content_height(), left + 6)

    def test_pending_detail_turns_columns_on(self):
        self.finish_luna()
        self.feed.start_detail(self.root, self.detail, "gpt-6.1-sol",
                               "en cola tras gpt-6-luna…")
        self.assertTrue(self.card.two_columns)
        self.assertFalse(self.card.right_scroll.isHidden())
        self.assertEqual(
            self.card.detail.header.full_text(),
            "gpt-6.1-sol · detalles · en cola tras gpt-6-luna…")
        self.feed.cancel_detail(self.detail)
        self.assertFalse(self.card.two_columns)
        self.assertTrue(self.card.right_scroll.isHidden())

    def test_alt_s_goes_to_right_slot_and_hides_detail(self):
        self.finish_luna()
        self.run_detail()
        card = self.card
        follow = object()
        self.feed.start_followup(self.root, follow, "gpt-6.1-sol · web")
        pump()
        self.assertFalse(card.pending_label.isHidden())
        self.assertIn("gpt-6.1-sol · web · pensando…",
                      card.pending_label.full_text())
        self.assertFalse(card.detail.isHidden())
        self.feed.update(follow, "Versión mejorada")
        self.feed.finish(follow, True, "Versión mejorada")
        self.feed.status(follow, "9.0s · caché 80%")
        pump(250)
        root, v1 = card.versions
        self.assertTrue(card.detail.isHidden())
        self.assertTrue(card.pending_label.isHidden())
        self.assertTrue(card.left_panel.isAncestorOf(root.body))
        self.assertIn("Di ahora", root.body.toPlainText())
        self.assertTrue(card.header_label.full_text().startswith("gpt-6-luna"))
        self.assertIn("caché 97%", card.header_label.full_text())
        self.assertTrue(card.right_panel.isAncestorOf(v1.body))
        self.assertEqual(v1.body.toPlainText().strip(), "Versión mejorada")
        self.assertFalse(card.right_header_label.isHidden())
        self.assertEqual(card.right_header_label.full_text(),
                         "gpt-6.1-sol · web · 9.0s · caché 80%")
        self.assertTrue(card.two_columns)

    def test_second_followup_folds_first_into_history(self):
        self.finish_luna()
        card = self.card
        first, second = object(), object()
        self.feed.start_followup(self.root, first, "sol-a")
        self.feed.update(first, "Primera mejora")
        self.feed.finish(first, True, "Primera mejora")
        self.feed.start_followup(self.root, second, "sol-b")
        self.feed.update(second, "Segunda mejora")
        self.feed.finish(second, True, "Segunda mejora")
        pump(250)
        root, v1, v2 = card.versions
        self.assertTrue(card.right_panel.isAncestorOf(v2.body))
        self.assertTrue(card.right_panel.isAncestorOf(card.history_container))
        self.assertFalse(card.history_container.isHidden())
        self.assertFalse(v1.row.isHidden())
        self.assertTrue(v1.content.isHidden())
        self.assertIn("Anterior · sol-a", v1.button.text())
        card.toggle_version(first)
        pump(80)
        self.assertFalse(v1.content.isHidden())
        self.assertIn("Primera", v1.body.toPlainText())
        card.toggle_version(first)
        pump(80)
        self.assertTrue(v1.content.isHidden())
        card.toggle_version(self.root)
        self.assertFalse(root.expanded)
        self.assertIn("Di ahora", root.body.toPlainText())
        self.assertEqual(card.right_header_label.full_text().split(" · ")[0],
                         "sol-b")

    def test_columns_scroll_independently_when_capped(self):
        self.finish_luna()
        self.run_detail(SOL + "\n\n" + LONG)
        self.feed.resize(1000, 320)
        pump(300)
        card = self.card
        self.assertLessEqual(card.height(), self.feed.viewport().height())
        self.assertGreater(card.right_scroll.verticalScrollBar().maximum(), 0)
        self.assertEqual(card.left_scroll.verticalScrollBar().maximum(), 0)
        self.assertEqual(card.right_scroll.verticalScrollBar().value(), 0)
        self.assertEqual(self.feed.verticalScrollBar().maximum(), 0)

    def test_streaming_does_not_scroll_columns(self):
        self.finish_luna()
        self.feed.resize(1000, 320)
        self.feed.start_detail(self.root, self.detail, "gpt-6.1-sol", "cola")
        self.feed.begin_detail(self.detail)
        for n in (10, 30, 60):
            self.feed.update(self.detail, "\n\n".join(
                f"- línea {i}" for i in range(n)))
            pump(120)
        self.assertEqual(self.card.right_scroll.verticalScrollBar().value(), 0)


class SideDiagramTests(unittest.TestCase):
    def setUp(self):
        self.feed = gui.AnswerFeed(font_px=13, side_by_side=True)
        self.feed.resize(1000, 800)
        self.feed.show()
        self.root = object()
        self.detail = object()
        self.card = self.feed.start(self.root, "Manual", "gpt-6-luna", "¿?")
        self.feed.update(self.root, LUNA)
        self.feed.finish(self.root, True, LUNA)
        self.feed.start_detail(self.root, self.detail, "gpt-6.1-sol", "cola")
        self.feed.begin_detail(self.detail)
        self.text = (f"### Detalles\n\n- uno\n\n### Diagrama\n\n"
                     f"```mermaid\n{F1}\n```")
        self.feed.update(self.detail, self.text[:30])
        self.feed.update(self.detail, self.text)
        self.feed.finish(self.detail, True, self.text)
        pump(250)

    def tearDown(self):
        self.feed.close()

    def test_detail_diagram_is_in_left_column(self):
        card = self.card
        self.assertEqual(len(card.side_stack.views), 1)
        view = card.side_stack.views[0]
        self.assertTrue(card.left_panel.isAncestorOf(view))
        self.assertFalse(card.right_panel.isAncestorOf(view))
        self.assertEqual(card.detail.stack.views, [])
        self.assertEqual(card.side_stack.caption.text(),
                         "Diagrama · gpt-6.1-sol")
        self.assertFalse(card.side_stack.caption.isHidden())
        body = card.detail.body.toPlainText()
        self.assertIn("uno", body)
        self.assertNotIn("Diagrama", body)
        self.assertNotIn("flowchart", body)
        left_x = view.mapTo(card, QPoint(0, 0)).x()
        right_x = card.right_scroll.mapTo(card, QPoint(0, 0)).x()
        self.assertLess(left_x, right_x)

    def test_open_fence_placeholder_is_left(self):
        card = self.card
        self.feed.update(self.detail, "### D\n\n```mermaid\nflowchart TD\n A")
        pump(150)
        self.assertEqual(card.side_stack.views, [])
        self.assertFalse(card.side_stack.placeholder.isHidden())
        self.assertTrue(card.left_panel.isAncestorOf(
            card.side_stack.placeholder))
        self.assertTrue(card.detail.stack.isHidden())

    def test_alt_s_without_diagram_removes_it(self):
        card = self.card
        follow = object()
        self.feed.start_followup(self.root, follow, "gpt-6.1-sol · web")
        pump(100)
        self.assertEqual(len(card.side_stack.views), 1)
        self.feed.update(follow, "Versión sin diagrama")
        self.feed.finish(follow, True, "Versión sin diagrama")
        pump(250)
        self.assertEqual(card.side_stack.views, [])
        self.assertTrue(card.side_stack.isHidden())
        self.assertTrue(card.detail.isHidden())

    def test_alt_s_with_diagram_replaces_it(self):
        card = self.card
        first_view = card.side_stack.views[0]
        follow = object()
        self.feed.start_followup(self.root, follow, "gpt-6.1-sol · web")
        text = f"Nueva\n\n```mermaid\n{F2}\n```"
        self.feed.update(follow, text)
        self.feed.finish(follow, True, text)
        pump(250)
        self.assertEqual(len(card.side_stack.views), 1)
        view = card.side_stack.views[0]
        self.assertIsNot(view, first_view)
        self.assertIn("rl", view.graph.nodes)
        self.assertNotIn("api", view.graph.nodes)
        self.assertTrue(card.left_panel.isAncestorOf(view))
        self.assertEqual(card.side_stack.caption.text(),
                         "Diagrama · gpt-6.1-sol · web")
        self.assertEqual(card.versions[1].stack.views, [])
        self.assertEqual(card.body.toPlainText().strip(), "Nueva")

    def test_root_own_diagram_stays_with_root(self):
        feed = gui.AnswerFeed(font_px=13, side_by_side=True)
        feed.resize(1000, 800)
        feed.show()
        root = object()
        card = feed.start(root, "Diagrama", "gpt-6-luna", "dibujar")
        text = f"- viñeta\n\n```mermaid\n{F2}\n```"
        feed.finish(root, True, text)
        pump(200)
        self.assertEqual(len(card.versions[0].stack.views), 1)
        self.assertEqual(card.side_stack.views, [])
        self.assertFalse(card.two_columns)
        feed.close()

    def test_single_column_keeps_diagram_in_section_with_caption(self):
        feed = gui.AnswerFeed(font_px=13)
        feed.resize(500, 800)
        feed.show()
        root, detail = object(), object()
        card = feed.start(root, "Manual", "gpt-6-luna", "¿?")
        feed.finish(root, True, LUNA)
        feed.start_detail(root, detail, "gpt-6.1-sol", "cola")
        feed.finish(detail, True, self.text)
        pump(250)
        self.assertEqual(len(card.detail.stack.views), 1)
        self.assertEqual(card.detail.stack.caption.text(), "Diagrama")
        self.assertNotIn("Diagrama", card.detail.body.toPlainText())
        feed.close()


class SingleColumnUnchangedTests(unittest.TestCase):
    def test_default_feed_is_single_column(self):
        feed = gui.AnswerFeed(font_px=15)
        root = object()
        card = feed.start(root, "Manual", "gpt-6-luna", "¿?")
        card.start_detail(object(), "gpt-6.1-sol", "cola")
        self.assertFalse(card.side_by_side)
        self.assertFalse(card.two_columns)
        self.assertFalse(hasattr(card, "left_scroll"))
        self.assertFalse(feed.side_by_side)
        self.assertFalse(feed.two_columns())


class OverlaySizingTests(unittest.TestCase):
    def overlay_with(self, detail):
        overlay = gui.CompactOverlay()
        self.addCleanup(overlay.close)
        root = object()
        overlay.feed.start(root, "Manual", "gpt-6-luna", "¿?")
        overlay.feed.update(root, LUNA)
        overlay.feed.finish(root, True, LUNA)
        if detail:
            overlay.feed.start_detail(root, object(), "gpt-6.1-sol", "cola")
        pump()
        return overlay

    def test_two_columns_use_more_width(self):
        available = APP.primaryScreen().availableGeometry()
        single = self.overlay_with(False)
        double = self.overlay_with(True)
        self.assertFalse(single.feed.two_columns())
        self.assertTrue(double.feed.two_columns())
        w1, h1, _ = single._desired_size(available)
        w2, h2, _ = double._desired_size(available)
        self.assertLessEqual(w1, 780)
        self.assertGreater(w2, w1)
        self.assertLessEqual(w2, available.width() - 16)
        self.assertLessEqual(w2, 1600)
        self.assertLessEqual(h2, round(available.height() * 0.92))
        self.assertLessEqual(h1, round(available.height() * 0.85))
        expected = min(max(900, round(available.width() * 0.72)), 1600,
                       available.width() - 16)
        self.assertEqual(w2, expected)

    def test_resize_keeps_overlay_on_screen(self):
        available = APP.primaryScreen().availableGeometry()
        overlay = self.overlay_with(True)
        overlay.move(available.right() - 100, available.bottom() - 100)
        overlay._resize_to_content()
        end = overlay._resize_animation.endValue()
        self.assertGreaterEqual(end.left(), available.left())
        self.assertLessEqual(end.right(), available.right())
        self.assertLessEqual(end.bottom(), available.bottom())
        self.assertGreaterEqual(end.top(), available.top())

    def test_live_lanes_rerendered_on_resize(self):
        overlay = gui.CompactOverlay()
        self.addCleanup(overlay.close)
        long_line = "palabra " * 60
        overlay.set_live("Entrevistador", long_line, final=True)
        overlay.show()
        pump()
        # El panel se ajusta solo al contenido (animación); fijar el ancho
        # para medir el re-renderizado a dos anchos distintos.
        overlay._resize_animation.stop()
        overlay.setFixedWidth(500)
        pump()
        narrow = overlay.interviewer_live.text()
        overlay.setFixedWidth(1400)
        pump()
        wide = overlay.interviewer_live.text()
        self.assertGreater(len(wide), len(narrow))


if __name__ == "__main__":
    unittest.main()


class LongQuestionWidthTests(unittest.TestCase):
    """Una pregunta larga del entrevistador no debe ensanchar las columnas
    más que su visor: antes la línea «…» fijaba el ancho mínimo al texto
    completo y la respuesta quedaba recortada en el panel Ctrl+I."""

    QUESTION = ("Okay, so now scale it. Redis is a single point of failure "
                "and you have fifty million users. How do you shard, what "
                "happens on a hot key like one huge customer hammering a "
                "single endpoint, and how do you keep the counters "
                "consistent across regions, and what do you page on?")

    def test_columns_never_wider_than_their_viewport(self):
        feed = gui.AnswerFeed(font_px=13, side_by_side=True)
        self.addCleanup(feed.close)
        feed.resize(900, 600)
        feed.show()
        root, detail = object(), object()
        card = feed.start(root, "Manual", "gpt-6-luna", self.QUESTION)
        feed.update(root, LUNA)
        feed.finish(root, True, LUNA)
        feed.start_detail(root, detail, "gpt-6.1-sol", "cola")
        feed.begin_detail(detail)
        feed.update(detail, SOL)
        feed.finish(detail, True, SOL)
        pump(300)
        self.assertLess(card.question_label.minimumSizeHint().width(), 100)
        self.assertLessEqual(card.left_panel.width(),
                             card.left_scroll.viewport().width())
        self.assertLessEqual(card.right_panel.width(),
                             card.right_scroll.viewport().width())
        self.assertLessEqual(card.minimumSizeHint().width(), 400)
        self.assertLessEqual(card.width(), feed.viewport().width())
        self.assertTrue(card.question_label.text().endswith("…"))

    def test_overlay_minimum_width_not_driven_by_question(self):
        overlay = gui.CompactOverlay()
        self.addCleanup(overlay.close)
        root = object()
        overlay.feed.start(root, "Manual", "gpt-6-luna", self.QUESTION)
        overlay.feed.update(root, LUNA)
        overlay.feed.finish(root, True, LUNA)
        overlay.feed.start_detail(root, object(), "gpt-6.1-sol", "cola")
        overlay.show()
        pump(300)
        self.assertLessEqual(overlay.minimumSizeHint().width(), 600)
        self.assertLessEqual(overlay.layout().minimumSize().width(), 600)

    def test_overlay_minimum_width_not_driven_by_live_transcript(self):
        """Las líneas «en directo» se eliden al ancho actual: si fijaran el
        mínimo del panel, este crecía hasta salirse de la pantalla."""
        overlay = gui.CompactOverlay()
        self.addCleanup(overlay.close)
        overlay.show()
        for _ in range(3):
            overlay.set_live("Entrevistador", self.QUESTION * 2, final=True)
            overlay.set_live("Tú", self.QUESTION, final=False)
        pump(200)
        self.assertLessEqual(overlay.minimumSizeHint().width(), 600)
        self.assertLessEqual(overlay.layout().minimumSize().width(), 600)
        self.assertLessEqual(overlay.interviewer_live.minimumSizeHint().width()
                             if overlay.interviewer_live.sizePolicy()
                             .horizontalPolicy() != gui.QSizePolicy.Ignored
                             else 0, 600)
