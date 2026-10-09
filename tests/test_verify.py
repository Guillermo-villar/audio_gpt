import os
os.environ.setdefault("AUDIO_GPT_NO_LOG", "1")
import threading
import time
import unittest
from unittest import mock

import audio_ring
import prompts
import verify


def ring_with(seconds, sr=1000):
    ring = audio_ring.PcmRing(sr)
    ring.append(b"\x01\x00" * int(seconds * sr))
    return ring


@mock.patch("verify.llm.log_call")
class SecondEarTests(unittest.TestCase):
    def ear(self, fn, **kw):
        ear = verify.SecondEar("k", transcribe=fn, **kw)
        self.addCleanup(ear.shutdown)
        return ear

    def test_submit_result_pending_wait(self, _log):
        gate = threading.Event()
        seen = {}

        def fake(wav, prompt):
            seen["prompt"] = prompt
            seen["wav"] = wav
            gate.wait(2)
            return " hola mundo "

        ear = self.ear(fake)
        self.assertTrue(ear.submit(("s", 1), ring_with(3), {"start": 0.5, "end": 2.0},
                                   "contexto previo"))
        self.assertTrue(ear.pending(("s", 1)))
        self.assertIsNone(ear.result(("s", 1)))
        self.assertTrue(ear.has(("s", 1)))
        gate.set()
        ear.wait([("s", 1)], 2)
        self.assertFalse(ear.pending(("s", 1)))
        self.assertEqual(ear.result(("s", 1)), "hola mundo")
        self.assertIn("contexto previo", seen["prompt"])
        self.assertTrue(seen["wav"].startswith(b"RIFF"))

    def test_wait_budget_is_total(self, _log):
        gate = threading.Event()
        ear = self.ear(lambda w, p: gate.wait(5) and "x")
        ear.submit("a", ring_with(3), {"start": 0, "end": 2}, "")
        ear.submit("b", ring_with(3), {"start": 0, "end": 2}, "")
        started = time.monotonic()
        ear.wait(["a", "b"], 0.3)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(ear.pending("a"))
        gate.set()

    def test_skips_short_and_evicted_and_missing(self, _log):
        calls = []
        ear = self.ear(lambda w, p: calls.append(1) or "x")
        self.assertFalse(ear.submit("a", ring_with(3), {"start": 1.0, "end": 1.05}, ""))
        self.assertFalse(ear.submit("b", ring_with(1), {"start": 5.0, "end": 6.0}, ""))
        self.assertFalse(ear.submit("c", ring_with(3), None, ""))
        small = audio_ring.PcmRing(1000, max_seconds=1)
        for _ in range(5):
            small.append(b"\x00\x00" * 1000)
        self.assertFalse(ear.submit("d", small, {"start": 0, "end": 1.5}, ""))
        self.assertEqual(calls, [])
        self.assertFalse(ear.has("a"))

    def frames(self, ear_factory, meta):
        import io
        import wave
        seen = {}

        def fake(wav, prompt):
            with wave.open(io.BytesIO(wav)) as wf:
                seen["frames"] = wf.getnframes()
            return "x"

        ear = ear_factory(fake)
        submitted = ear.submit("a", ring_with(5), meta, "")
        ear.wait(["a"], 2)
        return submitted, seen.get("frames")

    def test_word_bounds_preferred_and_pad_applied(self, _log):
        meta = {"start": 0.0, "end": 4.0, "words": [
            {"word": "a", "start": 1.0, "end": 1.2},
            {"word": "b", "start": 1.5, "end": 2.0}]}
        submitted, frames = self.frames(self.ear, meta)
        self.assertTrue(submitted)
        self.assertEqual(frames, 1240)

    def test_meta_bounds_when_words_lack_timestamps(self, _log):
        meta = {"start": 0.5, "end": 2.0, "words": [
            {"word": "a", "confidence": 0.9}]}
        submitted, frames = self.frames(self.ear, meta)
        self.assertTrue(submitted)
        self.assertEqual(frames, 1740)
        submitted, frames = self.frames(
            self.ear, {"start": 0.5, "end": 2.0})
        self.assertEqual(frames, 1740)

    def test_short_unpadded_span_skipped(self, _log):
        meta = {"start": 1.0, "end": 1.4}
        submitted, frames = self.frames(self.ear, meta)
        self.assertFalse(submitted)
        self.assertIsNone(frames)
        words_meta = {"start": 0.0, "end": 4.0, "words": [
            {"word": "a", "start": 1.0, "end": 1.4}]}
        self.assertFalse(self.frames(self.ear, words_meta)[0])

    def test_duplicate_id_not_resubmitted(self, _log):
        ear = self.ear(lambda w, p: "x")
        self.assertTrue(ear.submit("a", ring_with(3), {"start": 0, "end": 2}, ""))
        self.assertFalse(ear.submit("a", ring_with(3), {"start": 0, "end": 2}, ""))

    def test_reset_drops_in_flight_results(self, _log):
        gate = threading.Event()
        ear = self.ear(lambda w, p: gate.wait(2) and "viejo")
        ear.submit("a", ring_with(3), {"start": 0, "end": 2}, "")
        ear.reset()
        gate.set()
        time.sleep(0.2)
        self.assertIsNone(ear.result("a"))
        self.assertFalse(ear.has("a"))

    def test_transcribe_error_gives_none(self, log):
        def boom(wav, prompt):
            raise RuntimeError("timeout")

        ear = self.ear(boom)
        ear.submit("a", ring_with(3), {"start": 0, "end": 2}, "")
        ear.wait(["a"], 2)
        self.assertIsNone(ear.result("a"))
        entry = log.call_args.args[0]
        self.assertEqual(entry.kind, "second_ear")
        self.assertFalse(entry.ok)
        self.assertIn("timeout", entry.error)

    def test_context_trimmed_to_500(self, _log):
        seen = {}
        ear = self.ear(lambda w, p: seen.setdefault("p", p) and "x")
        ear.submit("a", ring_with(3), {"start": 0, "end": 2}, "z" * 900)
        ear.wait(["a"], 2)
        self.assertEqual(seen["p"].count("z"), 500)


class HelperTests(unittest.TestCase):
    def test_stt_languages(self):
        self.assertIsNone(verify.stt_languages(""))
        self.assertIsNone(verify.stt_languages(None))
        self.assertEqual(verify.stt_languages("es"), ["es", "en"])
        self.assertEqual(verify.stt_languages("en"), ["en"])
        self.assertEqual(verify.stt_languages("fr"), ["fr", "en"])

    def test_sanitize_keywords(self):
        out = verify.sanitize_keywords(
            [" Redis ", "redis", "", "a<b", "x>y", "li\nne", "c\rr", "Kafka"])
        self.assertEqual(out, ["Redis", "Kafka"])
        self.assertEqual(len(verify.sanitize_keywords(
            [f"t{i}" for i in range(100)])), 40)
        self.assertEqual(len(verify.sanitize_keywords(
            [f"t{i}" for i in range(100)], limit=5)), 5)

    def test_mark_low_confidence(self):
        words = [{"word": "great", "confidence": 0.4},
                 {"word": "limiter", "confidence": 0.95},
                 {"word": "x", "confidence": None},
                 {"word": "edge", "confidence": 0.6}]
        self.assertEqual(verify.mark_low_confidence("t", words),
                         "[great?] limiter x edge")
        self.assertEqual(verify.mark_low_confidence("texto", None), "texto")
        self.assertEqual(verify.mark_low_confidence("texto", []), "texto")

    def test_normalize(self):
        self.assertEqual(verify.normalize("<S0> Hola,  MUNDO!"), "hola mundo")
        self.assertEqual(verify.normalize("¿Cómo escalas el QPS?"),
                         "cómo escalas el qps")
        self.assertEqual(verify.normalize("rate-limiter_x"), "rate limiter x")
        self.assertEqual(verify.normalize(None), "")


class EquivalentTests(unittest.TestCase):
    def test_equal(self):
        self.assertTrue(verify.equivalent("Hola, mundo!", "hola mundo"))

    def test_trailing_boundary_words(self):
        self.assertTrue(verify.equivalent(
            "assume around ten requests we need it to work across",
            "assume around ten requests we need it to work across "
            "multiple regions."))

    def test_leading_boundary_words(self):
        self.assertTrue(verify.equivalent(
            "regions with redis", "multiple regions with redis"))
        self.assertTrue(verify.equivalent(
            "multiple regions with redis", "regions with redis"))

    def test_replacement_is_not_equivalent(self):
        self.assertFalse(verify.equivalent("great limiter", "rate limiter"))

    def test_interior_insertion_is_not_equivalent(self):
        self.assertFalse(verify.equivalent(
            "design a limiter for the api", "design a rate limiter for the api"))

    def test_long_boundary_insertion_is_not_equivalent(self):
        self.assertFalse(verify.equivalent(
            "work across", "work across multiple regions with redis"))
        self.assertTrue(verify.equivalent(
            "work across", "work across multiple regions with"))

    def test_reconcile_fast_path_uses_it(self):
        call = mock.Mock()
        out = verify.reconcile(
            [{"a": "x", "a_plain": "work across",
              "b": "work across multiple regions."}], [], call)
        self.assertTrue(out["skipped"])
        call.assert_not_called()


class ArbiterTests(unittest.TestCase):
    def test_input_exact(self):
        pairs = [{"a": "how [great?] limiter", "a_plain": "how great limiter",
                  "b": "how a rate limiter"},
                 {"a": "and scale", "a_plain": "and scale", "b": None}]
        text = verify.arbiter_input(pairs, ["Tú: hola", "Entrevistador: x"])
        self.assertEqual(
            text,
            prompts.ARBITER_CONTEXT_HEADER + "\nTú: hola\nEntrevistador: x\n\n"
            + prompts.ARBITER_ITEMS_HEADER
            + "\n[1] A: how [great?] limiter\n    B: how a rate limiter"
            + "\n[2] A: and scale\n    B: " + prompts.ARBITER_NO_B)
        self.assertIn("(ninguno)", verify.arbiter_input(pairs, []))

    def pair(self, a, b):
        return {"a": a, "a_plain": a, "b": b}

    GOOD = {"question": "q", "changed": True, "material": False,
            "corrections": ["a → b"]}

    def test_no_b_returns_none(self):
        call = mock.Mock()
        self.assertIsNone(verify.reconcile([self.pair("a", None)], [], call))
        self.assertIsNone(verify.reconcile([], [], call))
        call.assert_not_called()

    def test_fast_path_when_equal(self):
        call = mock.Mock()
        out = verify.reconcile(
            [self.pair("Hola, mundo", "hola mundo"), self.pair("x", None)],
            [], call)
        self.assertEqual(out, {"question": None, "changed": False,
                               "material": False, "corrections": [],
                               "skipped": True})
        call.assert_not_called()

    def test_arbiter_path(self):
        call = mock.Mock(return_value=dict(self.GOOD))
        out = verify.reconcile([self.pair("great limiter", "rate limiter")],
                               ["ctx"], call)
        self.assertEqual(out["question"], "q")
        self.assertFalse(out["skipped"])
        sent = call.call_args.args[0]
        self.assertIn("[1] A: great limiter", sent)
        self.assertIn("ctx", sent)

    def test_invalid_or_exception_gives_none(self):
        pairs = [self.pair("a", "b")]
        for bad in ({"question": "q"}, "texto", None, [],
                    dict(self.GOOD, changed="yes"),
                    dict(self.GOOD, corrections=[1]),
                    dict(self.GOOD, question="  ")):
            self.assertIsNone(verify.reconcile(pairs, [], lambda t: bad), bad)

        def boom(t):
            raise RuntimeError("x")

        self.assertIsNone(verify.reconcile(pairs, [], boom))

    @mock.patch("verify.llm.log_call")
    @mock.patch("verify.llm.get_client")
    def test_call_arbiter_request(self, get_client, log):
        response = mock.Mock(
            output_text='{"question": "q", "changed": false, '
                        '"material": false, "corrections": []}',
            status="completed", usage=None)
        client = get_client.return_value.with_options.return_value
        client.responses.create.return_value = response
        config = {"arbiter_model": "gpt-6-luna", "arbiter_prompt": "ARB",
                  "service_tier": "fast"}
        data = verify.call_arbiter("k", config, "texto")
        self.assertEqual(data["question"], "q")
        get_client.return_value.with_options.assert_called_with(timeout=6)
        kwargs = client.responses.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "gpt-6-luna")
        self.assertEqual(kwargs["instructions"], "ARB")
        self.assertEqual(kwargs["input"], "texto")
        self.assertEqual(kwargs["service_tier"], "fast")
        self.assertEqual(kwargs["reasoning"], {"effort": "none"})
        self.assertEqual(kwargs["max_output_tokens"], 400)
        fmt = kwargs["text"]["format"]
        self.assertEqual(fmt["type"], "json_schema")
        self.assertTrue(fmt["strict"])
        self.assertEqual(fmt["schema"], prompts.ARBITER_SCHEMA)
        self.assertEqual(log.call_args.args[0].kind, "arbiter")
        config["service_tier"] = "auto"
        verify.call_arbiter("k", config, "t")
        self.assertNotIn("service_tier",
                         client.responses.create.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
