import os
os.environ.setdefault("AUDIO_GPT_NO_LOG", "1")
import unittest

import copilot
import prompts

CONFIG = {
    "fast_prompt": "FAST", "detail_prompt": "DETAIL",
    "detail_alone_prompt": "ALONE", "smart_prompt": "DEEPER",
}


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class WindowTests(unittest.TestCase):
    def test_latest_interviewer(self):
        utts = ["Entrevistador: Hola", "Tú: sí", "Entrevistador: Diseña X",
                "Tú: vale"]
        self.assertEqual(copilot.latest_interviewer(utts), "Diseña X")
        self.assertEqual(copilot.latest_interviewer(["Tú: a"]),
                         "(transcript completo)")
        self.assertEqual(copilot.latest_interviewer([]),
                         "(transcript completo)")

    def test_window_from_fourth_last_interviewer(self):
        utts = ["Entrevistador: a", "Tú: t0", "Entrevistador: b",
                "Entrevistador: c", "Tú: t1", "Entrevistador: d",
                "Entrevistador: e", "Tú: t2"]
        self.assertEqual(copilot.question_window(utts), utts[2:])
        self.assertEqual(copilot.question_window(utts, max_interviewer=2),
                         utts[5:])

    def test_window_fewer_interviewer_lines(self):
        utts = ["Tú: a", "Entrevistador: b", "Tú: c"]
        self.assertEqual(copilot.question_window(utts), utts[1:])

    def test_window_no_interviewer(self):
        utts = [f"Tú: {i}" for i in range(10)]
        self.assertEqual(copilot.question_window(utts), utts[-6:])
        self.assertEqual(copilot.question_window([]), [])

    def test_window_char_cap(self):
        utts = ["Entrevistador: " + "x" * 100 for _ in range(4)]
        window = copilot.question_window(utts, max_chars=250)
        self.assertEqual(window, utts[-2:])
        huge = ["Entrevistador: " + "x" * 5000]
        self.assertEqual(copilot.question_window(huge, max_chars=100), huge)


class TailTests(unittest.TestCase):
    window = ["Entrevistador: ¿cómo escalas?", "Tú: con shards"]

    def test_fast(self):
        tail = copilot.fast_tail(CONFIG, self.window, [])
        self.assertEqual(tail[0], {"role": "developer", "content": "FAST"})
        self.assertEqual(tail[1]["role"], "user")
        self.assertEqual(
            tail[1]["content"],
            prompts.WINDOW_HEADER + "\n" + "\n".join(self.window))

    def test_fast_pinned_second_ear(self):
        tail = copilot.fast_tail(CONFIG, self.window, ["QPS 12k", "p99 200ms"],
                                 second_ear=["Entrevistador: scale"])
        parts = tail[1]["content"].split("\n\n")
        self.assertEqual(parts[0], prompts.PINNED_HEADER
                         + "\n- QPS 12k\n- p99 200ms")
        self.assertTrue(parts[1].startswith(prompts.WINDOW_HEADER))
        self.assertEqual(parts[2], prompts.SECOND_EAR_HEADER
                         + "\nEntrevistador: scale")

    def test_detail_with_fast(self):
        tail = copilot.detail_tail(CONFIG, self.window, ["f1"], "luna",
                                   "  **Di ahora:** hola  ", True)
        self.assertEqual(tail[0]["content"], "DETAIL")
        parts = tail[1]["content"].split("\n\n")
        self.assertEqual(parts[0], prompts.PINNED_HEADER + "\n- f1")
        self.assertTrue(parts[1].startswith(prompts.WINDOW_HEADER))
        self.assertEqual(
            parts[2],
            prompts.FAST_ANSWER_HEADER.format(model="luna")
            + "\n<<<\n**Di ahora:** hola\n>>>")

    def test_detail_alone(self):
        for ok, text in ((False, "x"), (True, "   "), (True, "")):
            tail = copilot.detail_tail(CONFIG, self.window, [], "luna",
                                       text, ok)
            self.assertEqual(tail[0]["content"], "ALONE")
            self.assertTrue(
                tail[1]["content"].endswith(prompts.FAST_ANSWER_FAILED))

    def test_detail_verified(self):
        verified = {"question": "How to scale reads?", "material": True}
        tail = copilot.detail_tail(CONFIG, self.window, [], "luna", "txt",
                                   True, verified)
        parts = tail[1]["content"].split("\n\n")
        self.assertEqual(
            parts[1], prompts.VERIFIED_HEADER + "\nHow to scale reads?\n"
            + prompts.VERIFIED_MATERIAL_NOTE)
        self.assertTrue(parts[2].startswith("Respuesta rápida"))
        alone = copilot.detail_tail(CONFIG, self.window, [], "luna", "",
                                    False, verified)
        self.assertNotIn(prompts.VERIFIED_MATERIAL_NOTE,
                         alone[1]["content"])
        nomat = copilot.detail_tail(CONFIG, self.window, [], "luna", "txt",
                                    True, {"question": "q"})
        self.assertNotIn(prompts.VERIFIED_MATERIAL_NOTE,
                         nomat[1]["content"])

    def test_deeper(self):
        previous = [("luna", "uno", False), ("sol", "dos", True)]
        tail = copilot.deeper_tail(CONFIG, "Pregunta", "Tú: a", previous,
                                   ["p"])
        self.assertEqual(tail[0]["content"], "DEEPER")
        parts = tail[1]["content"].split("\n\n")
        self.assertEqual(parts[0], prompts.PINNED_HEADER + "\n- p")
        self.assertEqual(
            parts[1], "Intervención del entrevistador a la que hay que "
            "responder:\nPregunta")
        self.assertEqual(parts[2], prompts.MINE_HEADER + "\nTú: a")
        self.assertEqual(parts[3], prompts.PREVIOUS_ANSWER_HEADER.format(
            model="luna") + "\n<<<\nuno\n>>>")
        self.assertEqual(parts[4], prompts.PREVIOUS_FOLLOWUP_HEADER.format(
            model="sol") + "\n<<<\ndos\n>>>")
        empty = copilot.deeper_tail(CONFIG, "Q", "", [], [])
        self.assertIn(prompts.MINE_HEADER + "\n(nada todavía)",
                      empty[1]["content"])


class CascadeTests(unittest.TestCase):
    def test_single_start(self):
        cascade = copilot.Cascade("k")
        self.assertFalse(cascade.take_detail_start())
        cascade.fast_finished(True, "txt")
        self.assertTrue(cascade.take_detail_start())
        self.assertFalse(cascade.take_detail_start())
        self.assertTrue(cascade.fast_ok)
        self.assertEqual(cascade.fast_text, "txt")

    def test_failed_fast_still_starts(self):
        cascade = copilot.Cascade("k")
        cascade.fast_finished(False, "")
        self.assertTrue(cascade.take_detail_start())
        self.assertFalse(cascade.fast_ok)

    def test_cancel(self):
        cascade = copilot.Cascade("k")
        cascade.cancel()
        cascade.fast_finished(True, "x")
        self.assertFalse(cascade.take_detail_start())
        self.assertTrue(cascade.cancelled)

    def test_verify_gating(self):
        clock = Clock()
        cascade = copilot.Cascade("k", verify_required=True,
                                  verify_deadline_s=3.5, clock=clock)
        cascade.fast_finished(True, "x")
        self.assertFalse(cascade.verify_done)
        self.assertFalse(cascade.take_detail_start())
        clock.now += 3.4
        self.assertFalse(cascade.take_detail_start())
        cascade.verify_finished({"question": "q"})
        self.assertEqual(cascade.verified, {"question": "q"})
        self.assertTrue(cascade.take_detail_start())

    def test_verify_deadline(self):
        clock = Clock()
        cascade = copilot.Cascade("k", verify_required=True, clock=clock)
        cascade.fast_finished(True, "x")
        self.assertFalse(cascade.verify_expired())
        clock.now += 3.5
        self.assertTrue(cascade.verify_expired())
        self.assertTrue(cascade.take_detail_start())
        self.assertIsNone(cascade.verified)

    def test_verify_not_required(self):
        self.assertTrue(copilot.Cascade("k").verify_done)


class PrewarmSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.sched = copilot.PrewarmScheduler(clock=self.clock)

    def test_debounce(self):
        self.sched.note_change()
        self.assertFalse(self.sched.due("luna", "a", 2000))
        self.clock.now += 1.2
        self.assertTrue(self.sched.due("luna", "a", 2000))

    def test_min_tokens(self):
        self.assertFalse(self.sched.due("luna", "a", 1099))
        self.assertTrue(self.sched.due("luna", "a", 1100))

    def test_signature_and_interval(self):
        self.sched.mark_sent("luna", "a")
        self.sched.mark_done("luna", True)
        self.clock.now += 20
        self.assertFalse(self.sched.due("luna", "a", 2000))
        self.assertTrue(self.sched.due("luna", "b", 2000))
        self.sched.mark_sent("luna", "b")
        self.sched.mark_done("luna", True)
        self.clock.now += 7
        self.assertFalse(self.sched.due("luna", "c", 2000))
        self.clock.now += 1
        self.assertTrue(self.sched.due("luna", "c", 2000))
        self.assertTrue(self.sched.due("sol", "c", 2000))

    def test_in_flight(self):
        self.sched.mark_sent("luna", "a")
        self.clock.now += 20
        self.assertFalse(self.sched.due("luna", "b", 2000))
        self.sched.mark_done("luna", True)
        self.assertTrue(self.sched.due("luna", "b", 2000))

    def test_disable_after_two_failures(self):
        self.sched.mark_sent("luna", "a")
        self.sched.mark_done("luna", False)
        self.assertFalse(self.sched.disabled("luna"))
        self.sched.mark_sent("luna", "b")
        self.sched.mark_done("luna", False)
        self.assertTrue(self.sched.disabled("luna"))
        self.clock.now += 100
        self.assertFalse(self.sched.due("luna", "c", 2000))
        self.assertFalse(self.sched.disabled("sol"))

    def test_success_resets_failures(self):
        self.sched.mark_sent("luna", "a")
        self.sched.mark_done("luna", False)
        self.sched.mark_sent("luna", "b")
        self.sched.mark_done("luna", True)
        self.sched.mark_sent("luna", "c")
        self.sched.mark_done("luna", False)
        self.assertFalse(self.sched.disabled("luna"))

    def test_reset(self):
        self.sched.mark_sent("luna", "a")
        self.sched.mark_done("luna", False)
        self.sched.mark_sent("luna", "b")
        self.sched.mark_done("luna", False)
        self.sched.reset()
        self.assertFalse(self.sched.disabled("luna"))
        self.assertTrue(self.sched.due("luna", "a", 2000))


class KeytermTests(unittest.TestCase):
    def test_merge(self):
        merged = copilot.merge_keyterms(["Redis", "mi cosa"],
                                        ["redis", "Kafka", "Redis", ""])
        self.assertEqual(merged, ["Redis", "mi cosa", "Kafka"])

    def test_limits(self):
        many = [f"t{i}" for i in range(300)]
        self.assertEqual(len(copilot.merge_keyterms([], many)), 100)
        words = ["a b c", "d e f", "g h i"]
        self.assertEqual(copilot.merge_keyterms([], words, max_tokens=12),
                         ["a b c", "d e f"])
        self.assertEqual(copilot.merge_keyterms(None, None), [])

    def test_sd_preset_within_deepgram_limits(self):
        merged = copilot.merge_keyterms([], prompts.SD_KEYTERMS)
        self.assertEqual(len(merged), len(prompts.SD_KEYTERMS))


if __name__ == "__main__":
    unittest.main()


class RealCallCountsAsWarmTests(unittest.TestCase):
    """Ctrl+Q escribe la misma caché que un pre-cacheo: tras la respuesta no
    hay que recalentar el mismo prefijo, pero sí cuando llega transcript
    nuevo. Modela también el caso «warm en vuelo + pregunta»."""

    def setUp(self):
        self.now = 1000.0
        self.sched = copilot.PrewarmScheduler(
            debounce_s=1.0, min_interval_s=8.0, min_tokens=100,
            clock=lambda: self.now)

    def _advance(self, s):
        self.now += s

    def test_real_call_suppresses_same_prefix(self):
        self.sched.note_change()
        self._advance(2)
        self.assertTrue(self.sched.due("luna", ("s", 10), 5000))
        self.sched.note_real_call("luna", ("s", 10))
        self._advance(20)
        self.assertFalse(self.sched.due("luna", ("s", 10), 5000))
        self.sched.note_change()
        self._advance(2)
        self.assertTrue(self.sched.due("luna", ("s", 12), 5000))

    def test_question_during_inflight_warm_does_not_wait(self):
        self.sched.note_change()
        self._advance(2)
        self.sched.mark_sent("luna", ("s", 10))
        self.sched.note_real_call("luna", ("s", 11))
        self.assertFalse(self.sched.due("luna", ("s", 11), 5000))
        self.sched.mark_done("luna", True)
        self._advance(20)
        self.assertFalse(self.sched.due("luna", ("s", 11), 5000),
                         "la pregunta ya calentó ese prefijo")
        self.sched.note_change()
        self._advance(2)
        self.assertTrue(self.sched.due("luna", ("s", 13), 5000))

    def test_stale_warm_completion_keeps_newer_signature(self):
        self.sched.mark_sent("luna", ("s", 10))
        self.sched.note_real_call("luna", ("s", 14))
        self.sched.mark_done("luna", True)
        self.assertEqual(
            self.sched._state("luna").last_signature, ("s", 14))

    def test_models_tracked_independently(self):
        self.sched.note_change()
        self._advance(2)
        self.sched.note_real_call("luna", ("s", 10))
        self.assertFalse(self.sched.due("luna", ("s", 10), 5000))
        self.assertTrue(self.sched.due("sol", ("t", 10), 5000))
