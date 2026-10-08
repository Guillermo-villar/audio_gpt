"""Casos de «reunión bajo presión» contra el backend simulado (mock_llm):
sin red ni tokens. Cubren las carreras y errores que no salen en pruebas
ligeras: Ctrl+Q repetido, Alt+S a mitad de stream, Sol caído, salidas
vacías, limpiar/parar a mitad de cascada, panel Ctrl+I abriéndose y
cerrándose mientras llegan deltas, y pre-caché en vuelo al preguntar."""

import os
os.environ.setdefault("AUDIO_GPT_NO_LOG", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import time
import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication

import api_client
import copilot
import gui
import llm
import mock_llm
import verify

APP = QApplication.instance() or QApplication([])

UTTS = [
    "Entrevistador: Hi, let's design a distributed rate limiter for our "
    "public API with fifty million daily active users.",
    "Tú: Acoto el alcance: límite por API key y por IP.",
    "Entrevistador: What algorithm would you pick and why?",
    "Tú: Token bucket en Redis con un script Lua.",
    "Entrevistador: Okay, so now scale it. Redis is a single point of "
    "failure. How do you shard and what happens on a hot key?",
    "Tú: Shardeo por API key con hashing consistente.",
]


def pump(cond=lambda: False, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        APP.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def finished(kind):
    return [c for c in mock_llm.calls(kind) if c["result"] is not None]


class PressureBase(unittest.TestCase):
    profile = "fast"
    speed = 30.0

    def setUp(self):
        mock_llm.reset()
        self._saved = (mock_llm.SPEED, mock_llm.PROFILE)
        mock_llm.SPEED = self.speed
        mock_llm.PROFILE = self.profile
        config = dict(api_client.DEFAULT_GPT_CONFIG)
        patches = [
            mock.patch.object(gui, "_install_ll_hook", return_value=None),
            mock.patch.object(
                gui, "SETTINGS_PATH",
                os.path.join(tempfile.mkdtemp(), "settings.json")),
            mock.patch.object(gui.GptClient, "load_config",
                              return_value=config),
            mock.patch.object(gui.ApiKeyManager, "load_api_key",
                              return_value="test-key"),
            mock.patch.object(llm, "stream", mock_llm.stream),
            mock.patch.object(llm, "get_client", mock_llm.get_client),
            mock.patch.object(llm, "prewarm", mock_llm.prewarm),
            mock.patch.object(llm, "ping", mock_llm.ping),
            mock.patch.object(llm, "log_call"),
            mock.patch.object(verify, "call_arbiter", mock_llm.call_arbiter),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.app = gui.WhisperApp()
        self.app.gpt_engine_combo.setCurrentIndex(
            self.app.gpt_engine_combo.findData("openai"))
        self.app.source_selector.setCurrentIndex(
            self.app.source_selector.findData("loopback"))
        self.addCleanup(self.shutdown)

    def shutdown(self):
        app = self.app
        app._cancel_cascades()
        for thread in list(app._gpt_threads) + list(app._verify_threads):
            thread.wait(5000)
        pump(lambda: not any(st.in_flight
                             for st in app._prewarm._states.values()), 15)
        if app._second_ear is not None:
            app._second_ear.shutdown()
        APP.processEvents()
        mock_llm.SPEED, mock_llm.PROFILE = self._saved
        mock_llm.reset()

    def fill(self):
        self.app._ctx.extend(UTTS)

    def ask(self):
        self.app._hk_answer_last()

    def card(self):
        return self.app.overlay.feed._cards[-1][1]

    def cards(self):
        return self.app.overlay.feed._cards

    def wait_detail_running(self, card):
        return pump(lambda: card.detail.state == "running"
                    and card.detail.text.strip(), 15)

    def wait_detail_settled(self, card):
        return pump(lambda: card.detail.state not in ("pending", "running"),
                    20)

    def assert_columns_fit(self, card):
        """Ninguna columna del panel Ctrl+I debe ser más ancha que su visor
        (si lo es, el texto se recorta). El mensaje lista al culpable."""
        from PySide6.QtWidgets import QWidget
        for panel, scroll in ((card.left_panel, card.left_scroll),
                              (card.right_panel, card.right_scroll)):
            limit = scroll.viewport().width()
            wide = []
            for w in panel.findChildren(QWidget):
                if w.isVisible() and w.minimumSizeHint().width() > limit:
                    getter = next((g for g in (
                        getattr(w, "full_text", None),
                        getattr(w, "toPlainText", None),
                        getattr(w, "text", None)) if callable(g)), None)
                    wide.append((type(w).__name__,
                                 w.minimumSizeHint().width(),
                                 getter()[:60] if getter else ""))
            self.assertLessEqual(panel.width(), limit, wide)

    def wait_root_done(self, card):
        return pump(lambda: card.versions[0].done, 15)


class RapidHotkeyTests(PressureBase):
    def test_triple_ctrl_q_leaves_one_card_with_one_live_cascade(self):
        self.fill()
        for _ in range(3):
            self.ask()
            pump(timeout=0.02)
        self.assertEqual(len(self.cards()), 1)
        card = self.card()
        self.assertTrue(self.wait_root_done(card))
        self.assertTrue(self.wait_detail_settled(card))
        self.assertEqual(len(self.cards()), 1)
        self.assertIn("Di ahora", card.versions[0].text)
        self.assertEqual(card.detail.state, "done")
        self.assertIn("Detalles", card.detail.text)
        ok_details = [c for c in finished("detail") if c["result"].ok]
        self.assertEqual(len(ok_details), 1, "solo la última cascada vive")

    def test_ctrl_q_while_sol_streams_cancels_old_detail(self):
        self.fill()
        self.ask()
        first = self.card()
        self.assertTrue(self.wait_detail_running(first))
        self.ask()
        second = self.card()
        self.assertIsNot(first, second)
        self.assertEqual(len(self.cards()), 1)
        self.assertTrue(self.wait_detail_settled(second))
        self.assertEqual(second.detail.state, "done")
        statuses = [c["result"].status for c in finished("detail")]
        self.assertEqual(statuses.count("cancelled"), 1, statuses)

    def test_alt_s_while_luna_streams_replaces_and_keeps_text_readable(self):
        mock_llm.SPEED = 4.0
        self.fill()
        self.ask()
        card = self.card()
        self.assertTrue(pump(lambda: card.versions[0].text.strip()
                             and not card.versions[0].done, 10))
        self.app._hk_smarter()
        self.assertEqual(len(card.versions), 2)
        mock_llm.SPEED = 40.0
        self.assertTrue(pump(lambda: card.versions[1].done, 25))
        self.assertTrue(card.versions[1].text.strip())
        self.assertIs(card.current_version, card.versions[1])
        self.assertTrue(pump(lambda: card.versions[0].done, 10))
        self.assertEqual(len(self.cards()), 1)

    def test_alt_s_after_sol_started_supersedes_detail(self):
        self.fill()
        self.ask()
        card = self.card()
        self.assertTrue(self.wait_detail_running(card))
        self.app._hk_smarter()
        self.assertTrue(pump(lambda: len(card.versions) == 2
                             and card.versions[1].done, 25))
        self.assertTrue(card.versions[1].text.strip())
        self.assertTrue(card.detail.isHidden()
                        or card.detail.state != "running")


class DegradedBackendTests(PressureBase):
    def test_empty_transcript_is_a_noop(self):
        self.ask()
        pump(timeout=0.2)
        self.assertEqual(self.cards(), [])
        self.assertEqual(mock_llm.calls("fast"), [])

    def test_clear_transcript_mid_cascade_then_ask_again(self):
        self.fill()
        self.ask()
        pump(timeout=0.03)
        self.app.clear_text()
        pump(lambda: finished("fast"), 10)
        pump(timeout=0.3)
        self.fill()
        self.ask()
        card = self.card()
        self.assertTrue(self.wait_root_done(card))
        self.assertIn("Di ahora", card.versions[0].text)

    def test_stop_capture_mid_cascade_keeps_answer(self):
        self.fill()
        self.ask()
        card = self.card()
        self.assertTrue(self.wait_detail_running(card))
        self.app.stop_continuous_mode()
        self.assertTrue(self.wait_detail_settled(card))
        self.assertIn("Di ahora", card.versions[0].text)
        self.assertTrue(card.detail.text.strip())

    def test_second_ctrl_q_on_unchanged_transcript_still_answers(self):
        self.fill()
        self.ask()
        first = self.card()
        self.assertTrue(self.wait_detail_settled(first))
        self.ask()
        second = self.card()
        self.assertTrue(self.wait_root_done(second))
        self.assertIn("Di ahora", second.versions[0].text)


class FlakyBackendTests(PressureBase):
    profile = "flaky"

    def test_errors_and_empty_outputs_never_wedge_the_app(self):
        self.fill()
        seen_error = seen_ok = False
        for _ in range(6):
            self.ask()
            card = self.card()
            self.assertTrue(self.wait_root_done(card))
            self.assertTrue(self.wait_detail_settled(card))
            root = card.versions[0]
            if root.error_text or card.detail.error_text:
                seen_error = True
            if root.text.strip() and card.detail.state == "done":
                seen_ok = True
            header = card.header_label.full_text()
            self.assertTrue(header.strip())
            if root.error_text:
                self.assertIn("error", header)
        self.assertTrue(seen_error, "el perfil flaky debe fallar alguna vez")
        self.assertTrue(seen_ok, "y la app debe seguir contestando")
        self.assertEqual(len(self.cards()), 1)


class BurstRenderingTests(PressureBase):
    profile = "burst"

    def test_single_char_luna_and_single_delta_sol_render_in_overlay(self):
        self.fill()
        self.app._hk_toggle_compact()
        self.ask()
        card = self.card()
        self.assertTrue(self.wait_root_done(card))
        self.assertTrue(self.wait_detail_settled(card))
        pump(timeout=0.3)
        self.assertIn("Di ahora", card.versions[0].body.toPlainText())
        self.assertIn("Detalles", card.detail.body.toPlainText())
        self.assert_columns_fit(card)


class OverlayToggleTests(PressureBase):
    speed = 4.0

    def test_toggle_ctrl_i_while_both_answers_stream(self):
        self.fill()
        self.ask()
        card = self.card()
        overlay = self.app.overlay
        self.app._hk_toggle_compact()
        self.assertTrue(overlay.isVisible())
        pump(timeout=0.08)
        self.app._hk_toggle_compact()
        self.assertTrue(pump(lambda: not overlay.isVisible(), 2),
                        "el ocultado es diferido ~450 ms")
        pump(timeout=0.08)
        self.app._hk_toggle_compact()
        self.assertTrue(overlay.isVisible())
        self.assertTrue(self.wait_root_done(card))
        self.app._hk_toggle_compact()
        self.app._hk_toggle_compact()
        pump(timeout=0.6)
        self.assertTrue(overlay.isVisible(),
                        "doble Ctrl+I rápido = cancelar el ocultado")
        mock_llm.SPEED = 40.0
        self.assertTrue(self.wait_detail_settled(card))
        pump(timeout=0.3)
        self.assertTrue(overlay.isVisible())
        self.assertTrue(card.two_columns)
        self.assertIn("Di ahora", card.versions[0].body.toPlainText())
        self.assertIn("Detalles", card.detail.body.toPlainText())
        self.assert_columns_fit(card)


class PrewarmRaceTests(PressureBase):
    def _arm_prewarm(self):
        app = self.app
        app._prewarm = copilot.PrewarmScheduler(
            debounce_s=0, min_interval_s=0, min_tokens=0)
        app.is_continuous_mode = True
        app._prewarm_key = "test-key"
        app._prewarm_cfg = dict(api_client.DEFAULT_GPT_CONFIG)
        app.prewarm_checkbox.setChecked(True)
        self.fill()

    def test_prewarm_done_then_question_hits_cache_and_no_rewarm(self):
        self._arm_prewarm()
        self.app._prewarm_tick()
        self.assertTrue(pump(lambda: len(finished("prewarm")) >= 2, 10))
        self.ask()
        card = self.card()
        self.assertTrue(self.wait_root_done(card))
        fast = finished("fast")[-1]["result"]
        # Solo la cola dinámica (pregunta, pinned, segundo oído) va sin caché.
        self.assertGreater(fast.cached_tokens, 0.6 * fast.input_tokens)
        self.assertGreater(fast.cached_tokens, 1000)
        before = len(finished("prewarm"))
        self.app._prewarm_tick()
        pump(timeout=0.3)
        self.assertEqual(len(finished("prewarm")), before,
                         "la pregunta ya calentó ese prefijo")
        self.app._ctx.append("Entrevistador: and multi-region?")
        self.app._prewarm_tick()
        self.assertTrue(pump(lambda: len(finished("prewarm")) > before, 10))

    def test_question_during_inflight_prewarm_does_not_wait(self):
        self._arm_prewarm()
        with mock.patch.dict(mock_llm.PREWARM_S, {"luna": 90.0, "sol": 90.0}):
            self.app._prewarm_tick()
            self.ask()
            card = self.card()
            self.assertTrue(self.wait_root_done(card))
            self.assertEqual(finished("prewarm"), [],
                             "la respuesta llegó con el warm aún en vuelo")
            self.assertIn("Di ahora", card.versions[0].text)
            fast = finished("fast")[-1]["result"]
            self.assertEqual(fast.cached_tokens, 0)
            self.assertTrue(pump(lambda: len(finished("prewarm")) >= 2, 15))
        self.assertTrue(self.wait_detail_settled(card))


if __name__ == "__main__":
    unittest.main()
