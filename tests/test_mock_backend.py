import os
os.environ.setdefault("AUDIO_GPT_NO_LOG", "1")

import threading
import time
import unittest
from unittest import mock

import llm
import mock_llm
from api_client import DEFAULT_GPT_CONFIG

UTTS = [f"Entrevistador: línea {i} sobre rate limiting" for i in range(6)]
TAIL = [{"role": "user", "content": "Entrevistador: design a rate limiter"}]


def luna():
    return llm.luna_profile(dict(DEFAULT_GPT_CONFIG), "")


def sol():
    return llm.sol_profile(dict(DEFAULT_GPT_CONFIG), "")


class MockStreamTests(unittest.TestCase):
    """El simulador reproduce la forma del stream real: TTFT, deltas
    acumulativos, estados, cancelación, errores y texto vacío."""

    def setUp(self):
        mock_llm.reset()
        self._speed = mock_llm.SPEED
        self._profile = mock_llm.PROFILE
        mock_llm.SPEED = 50.0
        mock_llm.PROFILE = "fast"

    def tearDown(self):
        mock_llm.SPEED = self._speed
        mock_llm.PROFILE = self._profile
        mock_llm.reset()

    def test_luna_stream_is_cumulative_and_fast(self):
        seen = []
        kwargs = llm.request_kwargs(luna(), UTTS, TAIL)
        result = mock_llm.stream(None, kwargs, on_delta=seen.append,
                                 kind="fast")
        self.assertTrue(result.ok)
        self.assertGreater(len(seen), 5, "varios deltas, no uno solo")
        for a, b in zip(seen, seen[1:]):
            self.assertTrue(b.startswith(a), "cada delta acumula")
        self.assertEqual(seen[-1], result.text)
        self.assertIn("Di ahora", result.text)
        self.assertIsNotNone(result.ttft)
        self.assertLess(result.ttft, result.total)

    def test_sol_emits_search_status_then_text(self):
        statuses = []
        kwargs = llm.request_kwargs(sol(), UTTS, TAIL, tool_choice="auto")
        result = mock_llm.stream(None, kwargs, on_status=statuses.append,
                                 kind="detail")
        self.assertTrue(result.ok)
        self.assertEqual(statuses, ["buscando en la web…",
                                    "leyendo resultados…"])
        self.assertIn("### Detalles", result.text)
        self.assertIn("```mermaid", result.text)

    def test_cancel_mid_stream_returns_partial(self):
        cancel = threading.Event()
        seen = []

        def on_delta(text):
            seen.append(text)
            if len(seen) == 3:
                cancel.set()
        kwargs = llm.request_kwargs(luna(), UTTS, TAIL)
        result = mock_llm.stream(None, kwargs, on_delta=on_delta,
                                 cancel=cancel, kind="fast")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(len(seen), 3)
        self.assertEqual(result.text, seen[-1])

    def test_cancel_before_first_token(self):
        cancel = threading.Event()
        cancel.set()
        kwargs = llm.request_kwargs(sol(), UTTS, TAIL)
        result = mock_llm.stream(None, kwargs, cancel=cancel, kind="detail")
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.text, "")

    def test_flaky_profile_mixes_errors_empty_and_success(self):
        mock_llm.PROFILE = "flaky"
        outcomes = []
        for _ in range(10):
            kwargs = llm.request_kwargs(luna(), UTTS, TAIL)
            r = mock_llm.stream(None, kwargs, kind="fast")
            outcomes.append((r.ok, r.status, bool(r.text)))
        self.assertIn((True, "completed", True), outcomes)
        self.assertTrue(any(not ok and text for ok, _, text in outcomes),
                        "error a mitad con texto parcial")
        self.assertTrue(any(status == "empty" for _, status, _ in outcomes))
        detail = [mock_llm.stream(None, llm.request_kwargs(sol(), UTTS, TAIL),
                                  kind="detail") for _ in range(4)]
        self.assertTrue(any(not r.ok and not r.text for r in detail),
                        "Sol caído antes del primer token")

    def test_burst_profile_single_char_and_single_delta(self):
        mock_llm.PROFILE = "burst"
        seen = []
        mock_llm.stream(None, llm.request_kwargs(luna(), UTTS, TAIL),
                        on_delta=seen.append, kind="fast")
        self.assertEqual(len(seen[0]), 1)
        self.assertGreater(len(seen), 100)
        seen = []
        mock_llm.stream(None, llm.request_kwargs(sol(), UTTS, TAIL),
                        on_delta=seen.append, kind="detail")
        self.assertEqual(len(seen), 1)

    def test_speed_scales_timing(self):
        kwargs = llm.request_kwargs(luna(), UTTS, TAIL)
        mock_llm.SPEED = 50.0
        t0 = time.monotonic()
        mock_llm.stream(None, kwargs, kind="fast")
        fast = time.monotonic() - t0
        mock_llm.SPEED = 10.0
        t0 = time.monotonic()
        mock_llm.stream(None, kwargs, kind="fast")
        slow = time.monotonic() - t0
        self.assertGreater(slow, fast * 2)


class MockCacheTests(unittest.TestCase):
    """La caché simulada sigue las reglas documentadas: prefijo cacheado
    más largo que coincida; un warm viejo nunca cambia la respuesta."""

    def setUp(self):
        mock_llm.reset()
        self._speed = mock_llm.SPEED
        mock_llm.SPEED = 50.0

    def tearDown(self):
        mock_llm.SPEED = self._speed
        mock_llm.reset()

    def test_prewarm_then_question_hits_cache(self):
        profile = luna()
        cold = mock_llm.stream(None, llm.request_kwargs(profile, UTTS, TAIL),
                               kind="fast")
        self.assertEqual(cold.cached_tokens, 0)
        mock_llm.reset()
        warm = mock_llm.prewarm("k", profile, UTTS)
        self.assertTrue(warm.ok)
        self.assertGreater(warm.cache_write_tokens, 0)
        real = mock_llm.stream(None, llm.request_kwargs(profile, UTTS, TAIL),
                               kind="fast")
        self.assertGreater(real.cached_tokens, 0.8 * real.input_tokens)

    def test_question_after_new_lines_reuses_longest_prefix(self):
        profile = luna()
        mock_llm.prewarm("k", profile, UTTS[:4])
        newer = UTTS + ["Tú: y esto es nuevo"]
        real = mock_llm.stream(None, llm.request_kwargs(profile, newer, TAIL),
                               kind="fast")
        warmed = llm.estimate_tokens(profile.stable + "".join(UTTS[:4]))
        self.assertGreaterEqual(real.cached_tokens, warmed * 0.9)
        self.assertLess(real.cached_tokens, real.input_tokens)

    def test_stale_prewarm_cannot_change_answer(self):
        profile = luna()
        kwargs = llm.request_kwargs(profile, UTTS, TAIL)
        with mock.patch.dict(mock_llm.PREWARM_S, {"luna": 5.0}):
            t = threading.Thread(target=mock_llm.prewarm,
                                 args=("k", profile, UTTS[:3]))
            t.start()
            real = mock_llm.stream(None, kwargs, kind="fast")
            t.join()
        self.assertTrue(real.ok)
        self.assertIn("Di ahora", real.text)
        kinds = [c["kind"] for c in mock_llm.calls()]
        self.assertEqual(kinds, ["fast", "prewarm"], "la pregunta no espera")

    def test_models_do_not_share_cache(self):
        mock_llm.prewarm("k", luna(), UTTS)
        real = mock_llm.stream(None, llm.request_kwargs(sol(), UTTS, TAIL),
                               kind="detail")
        self.assertEqual(real.cached_tokens, 0)


if __name__ == "__main__":
    unittest.main()
