import os
os.environ.setdefault("AUDIO_GPT_NO_LOG", "1")
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import api_client
import audio_ring
import gui
import llm
import prompts
import verify

APP = QApplication.instance() or QApplication([])

LUNA_TEXT = "**Di ahora:** Luna dice que hay que usar una caché."
SOL_TEXT = "### Detalles\n\n- Sol añade el detalle."
UTTS = [
    "Entrevistador: Vamos a diseñar un limitador de tasa.",
    "Tú: Perfecto, empiezo por los requisitos.",
]
A_TEXT = "how would you scale the rite limiter"
B_TEXT = "How would you scale the rate limiter"


def pump(cond=lambda: False, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        APP.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class FakeStream:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, client, kwargs, *, on_delta=None, on_status=None,
                 cancel=None, kind=""):
        with self.lock:
            self.calls.append((kind, kwargs))
        text = LUNA_TEXT if kind == "fast" else SOL_TEXT
        if on_delta:
            on_delta(text)
        return llm.CallResult(
            ok=True, text=text, status="completed", kind=kind,
            model=kwargs.get("model", ""), ttft=0.5, total=1.2,
            input_tokens=1000, cached_tokens=900)

    def of(self, kind):
        with self.lock:
            return [kw for k, kw in self.calls if k == kind]


def tail_text(kwargs):
    return kwargs["input"][-1]["content"]


class AppFlowTests(unittest.TestCase):
    overrides = {}

    def setUp(self):
        self.stream = FakeStream()
        config = dict(api_client.DEFAULT_GPT_CONFIG)
        config.update(self.overrides)
        self.release = threading.Event()
        self.arbiter_calls = []
        self.arbiter_result = {
            "question": "How would you scale the rate limiter?",
            "changed": True, "material": True,
            "corrections": ["rite → rate"]}
        patches = [
            mock.patch.object(gui, "_install_ll_hook", return_value=None),
            mock.patch.object(
                gui, "SETTINGS_PATH",
                os.path.join(tempfile.mkdtemp(), "settings.json")),
            mock.patch.object(gui.GptClient, "load_config",
                              return_value=config),
            mock.patch.object(gui.ApiKeyManager, "load_api_key",
                              return_value="test-key"),
            mock.patch.object(llm, "stream", self.stream),
            mock.patch.object(llm, "get_client", return_value=object()),
            mock.patch.object(llm, "prewarm"),
            mock.patch.object(llm, "ping"),
            mock.patch.object(llm, "log_call"),
            mock.patch.object(verify, "call_arbiter", self.fake_arbiter),
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

    def fake_arbiter(self, api_key, config, text):
        self.arbiter_calls.append(text)
        self.release.wait(10)
        return dict(self.arbiter_result)

    def shutdown(self):
        self.release.set()
        app = self.app
        for thread in list(app._gpt_threads) + list(app._verify_threads):
            thread.wait(3000)
        if app._second_ear is not None:
            app._second_ear.shutdown()
        APP.processEvents()

    def card(self):
        return self.app.gpt_output._cards[-1][1]

    def fill_plain(self):
        self.app._ctx.extend(UTTS)

    def enable_second_ear(self, transcribe=lambda wav, prompt: B_TEXT):
        app = self.app
        app._ctx.extend(UTTS)
        app._rings = {"": audio_ring.PcmRing(16000)}
        app._rings[""].append(b"\x01\x00" * 16000 * 6)
        app._second_ear = verify.SecondEar("k", transcribe=transcribe)
        count = len(A_TEXT.split())
        meta = {"start": 0.5, "end": 3.0, "words": [
            {"word": w, "start": 0.5 + i * 2.5 / count,
             "end": 0.5 + (i + 1) * 2.5 / count,
             "confidence": 0.3 if w == "rite" else 0.95}
            for i, w in enumerate(A_TEXT.split())]}
        app._append_transcript(A_TEXT, True, "", meta)
        utt_id = (app._ear_session, len(app._ctx) - 1)
        self.assertTrue(app._second_ear.has(utt_id))
        self.assertTrue(pump(lambda: app._second_ear.result(utt_id)))
        return utt_id

    def test_no_auto_answer_controls_or_triggers(self):
        app = self.app
        self.assertFalse(hasattr(app, "auto_gpt_checkbox"))
        self.assertFalse(hasattr(app, "manual_gpt_checkbox"))
        self.assertFalse(hasattr(app, "gate_fired"))
        app._append_transcript(
            "how would you scale the write path for this?", False, "")
        app._append_transcript(
            "how would you scale the write path for this?", True, "")
        pump(lambda: False, 0.3)
        self.assertEqual(self.stream.calls, [])
        self.assertEqual(app._answers, [])

    def test_luna_then_sol(self):
        self.fill_plain()
        self.app.send_to_gpt()
        card = self.card()
        self.assertTrue(pump(lambda: card.detail.state == "done"))
        fast = self.stream.of("fast")
        detail = self.stream.of("detail")
        self.assertEqual((len(fast), len(detail)), (1, 1))
        self.assertEqual(fast[0]["model"], "gpt-6-luna")
        self.assertEqual(detail[0]["model"], "gpt-6.1-sol")
        self.assertIn(LUNA_TEXT, tail_text(detail[0]))
        self.assertIn(prompts.FAST_ANSWER_HEADER.format(model="gpt-6-luna"),
                      tail_text(detail[0]))
        self.assertNotIn(prompts.VERIFIED_HEADER, tail_text(detail[0]))
        self.assertNotIn(prompts.SECOND_EAR_HEADER, tail_text(fast[0]))
        self.assertEqual(card.body.toPlainText().strip()[:8], "Di ahora")
        self.assertIn("Sol añade", card.detail.body.toPlainText())
        self.assertIn("caché 90%", card.header_label.full_text())
        self.assertIn("caché 90%", card.detail.header.full_text())
        self.assertTrue(card.verify_label.isHidden())
        self.assertTrue(self.app.send_to_gpt_button.isEnabled())

    def test_second_ear_and_material_arbiter(self):
        self.release.set()
        self.enable_second_ear()
        self.app.send_to_gpt()
        card = self.card()
        self.assertTrue(pump(lambda: card.detail.state == "done"))
        fast = self.stream.of("fast")[0]
        detail = self.stream.of("detail")[0]
        self.assertIn(prompts.SECOND_EAR_HEADER, tail_text(fast))
        self.assertIn("Entrevistador: " + B_TEXT, tail_text(fast))
        self.assertIn(prompts.VERIFIED_HEADER, tail_text(detail))
        self.assertIn("How would you scale the rate limiter?",
                      tail_text(detail))
        self.assertIn(prompts.VERIFIED_MATERIAL_NOTE, tail_text(detail))
        self.assertEqual(len(self.arbiter_calls), 1)
        self.assertIn("[1] A: how would you scale the [rite?] limiter",
                      self.arbiter_calls[0])
        self.assertFalse(card.verify_label.isHidden())
        self.assertTrue(card.verify_label.text().startswith("⚠ Luna pudo oír"))
        self.assertEqual(card.verify_label.toolTip(), "rite → rate")

    def test_verification_survives_stopping_capture(self):
        self.release.set()
        self.enable_second_ear()
        self.app.is_continuous_mode = True
        ear = self.app._second_ear
        self.app.stop_continuous_mode()
        self.assertIs(self.app._second_ear, ear)
        self.assertEqual(self.app._rings, {})
        self.assertTrue(self.app._ear_meta)
        self.app.send_to_gpt()
        card = self.card()
        self.assertTrue(pump(lambda: card.detail.state == "done"))
        self.assertIn(prompts.VERIFIED_HEADER,
                      tail_text(self.stream.of("detail")[0]))
        self.assertTrue(card.verify_label.text().startswith("⚠"))

    def test_equal_transcripts_skip_arbiter(self):
        self.enable_second_ear(lambda wav, prompt: A_TEXT.upper() + ".")
        self.app.send_to_gpt()
        card = self.card()
        self.assertTrue(pump(lambda: card.detail.state == "done"))
        self.assertEqual(self.arbiter_calls, [])
        fast = self.stream.of("fast")[0]
        self.assertNotIn(prompts.SECOND_EAR_HEADER, tail_text(fast))
        self.assertNotIn(prompts.VERIFIED_HEADER,
                         tail_text(self.stream.of("detail")[0]))
        self.assertEqual(card.verify_label.text(), "✓ transcripción verificada")

    def test_unanswering_arbiter_does_not_block_detail(self):
        self.app._ctx.clear()
        with mock.patch.object(
                gui.GptClient, "load_config",
                return_value=dict(api_client.DEFAULT_GPT_CONFIG,
                                  verify_deadline_s=0.3,
                                  verify_wait_s=0.1)):
            self.enable_second_ear()
            started = time.monotonic()
            self.app.send_to_gpt()
            card = self.card()
            self.assertTrue(pump(lambda: card.detail.state == "done", 4.0))
        self.assertLess(time.monotonic() - started, 3.0)
        detail = self.stream.of("detail")
        self.assertEqual(len(detail), 1)
        self.assertNotIn(prompts.VERIFIED_HEADER, tail_text(detail[0]))
        self.assertTrue(card.verify_label.isHidden())
        self.release.set()
        self.assertTrue(pump(lambda: not card.verify_label.isHidden()))
        self.assertIn("Luna pudo oír", card.verify_label.text())
        self.assertEqual(len(self.stream.of("detail")), 1)

    def test_second_ask_cancels_pending_detail(self):
        self.enable_second_ear()
        self.app.send_to_gpt()
        first_card = self.card()
        first_cascade = next(iter(self.app._cascades.values()))
        self.assertTrue(pump(lambda: self.stream.of("fast")))
        pump(lambda: False, 0.3)
        self.assertEqual(first_card.detail.state, "pending")
        self.assertEqual(self.stream.of("detail"), [])

        self.app.send_to_gpt()
        self.assertTrue(first_cascade.cancelled)
        self.assertEqual(len(self.app._cascades), 1)
        self.assertEqual(len(self.app.gpt_output._cards), 1)
        new_card = self.card()
        self.assertIsNot(new_card, first_card)
        self.release.set()
        self.assertTrue(pump(lambda: new_card.detail.state == "done"))
        pump(lambda: False, 0.3)
        self.assertEqual(len(self.stream.of("detail")), 1)
        self.assertFalse(new_card.detail.isHidden())
        self.assertEqual(
            sum(1 for _, c in self.app.gpt_output._cards
                if not c.detail.isHidden()), 1)


if __name__ == "__main__":
    unittest.main()
