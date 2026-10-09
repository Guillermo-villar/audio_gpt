import os
os.environ.setdefault("AUDIO_GPT_NO_LOG", "1")
import threading
import unittest
from types import SimpleNamespace as NS
from unittest import mock

import httpx2
import openai

import llm
import prompts

CONFIG = {
    "model": "gpt-6-luna", "reasoning_effort": "none", "service_tier": "fast",
    "fast_verbosity": "low", "max_tokens": 2000,
    "detail_model": "gpt-6.1-sol", "detail_reasoning_effort": "medium",
    "smart_service_tier": "fast", "detail_verbosity": "medium",
    "smart_web_search": True, "detail_max_tokens": 6000,
    "system_prompt": "SYS", "format_prompt": "FMT",
}
UTTS = [f"Entrevistador: linea {i}" for i in range(40)]
TAIL = [{"role": "developer", "content": "MODE"},
        {"role": "user", "content": "ventana"}]


def bad_request(message):
    request = httpx2.Request("POST", "https://x/v1/responses")
    response = httpx2.Response(400, request=request)
    return openai.BadRequestError(message, response=response, body=None)


def event(type_, **kw):
    return NS(type=type_, **kw)


class FakeStream:
    def __init__(self, events, raises=None):
        self.events = events
        self.raises = raises
        self.closed = False

    def __iter__(self):
        for e in self.events:
            yield e
        if self.raises:
            raise self.raises

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.responses = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def completed(**usage):
    return event("response.completed", response=NS(
        status="completed",
        usage=NS(input_tokens=1000, output_tokens=50,
                 input_tokens_details=NS(cached_tokens=970,
                                         cache_write_tokens=30))))


class BuildInputTests(unittest.TestCase):
    def test_shape(self):
        items = llm.build_input("STABLE", UTTS, TAIL)
        self.assertEqual(items[0], {"role": "developer", "content": [{
            "type": "input_text", "text": "STABLE",
            "prompt_cache_breakpoint": {"mode": "explicit"}}]})
        blocks = items[1]["content"]
        self.assertEqual(items[1]["role"], "user")
        self.assertEqual(blocks[0],
                         {"type": "input_text",
                          "text": prompts.TRANSCRIPT_HEADER})
        self.assertEqual(len(blocks), 41)
        marked = [i for i, b in enumerate(blocks)
                  if "prompt_cache_breakpoint" in b]
        self.assertEqual(marked, list(range(9, 41)))
        self.assertEqual(blocks[1]["text"], UTTS[0] + "\n")
        self.assertEqual(items[2:], TAIL)

    def test_few_utterances_all_marked(self):
        blocks = llm.build_input("S", UTTS[:3])[1]["content"]
        self.assertNotIn("prompt_cache_breakpoint", blocks[0])
        self.assertTrue(all("prompt_cache_breakpoint" in b
                            for b in blocks[1:]))

    def test_no_transcript_message_when_empty(self):
        items = llm.build_input("S", [], TAIL)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[1], TAIL[0])


class RequestKwargsTests(unittest.TestCase):
    def setUp(self):
        self.luna = llm.luna_profile(CONFIG, "")
        self.sol = llm.sol_profile(CONFIG, "")

    def test_profiles(self):
        self.assertEqual(self.luna.model, "gpt-6-luna")
        self.assertEqual(self.luna.effort, "none")
        self.assertEqual(self.luna.stable, "SYS\n\nFMT")
        self.assertEqual(self.sol.tools, ({"type": "web_search"},))
        self.assertEqual(self.sol.max_output_tokens, 6000)
        withbrief = llm.luna_profile(CONFIG, "mi brief")
        self.assertIn("mi brief", withbrief.stable)

    def test_prewarm_and_real_share_prefix(self):
        for profile in (self.luna, self.sol):
            real = llm.request_kwargs(profile, UTTS, TAIL)
            warm = llm.request_kwargs(profile, UTTS, TAIL, prewarm=True)
            for key in ("model", "reasoning", "text",
                        "tools"):
                self.assertEqual(real.get(key), warm.get(key), key)
            self.assertEqual(real["input"][:2], warm["input"][:2])
            self.assertEqual(real["prompt_cache_options"],
                             {"mode": "explicit"})
            self.assertEqual(warm["prompt_cache_options"],
                             {"mode": "explicit", "prewarm": True})
            self.assertEqual(len(warm["input"]), 2)
            self.assertNotIn("max_output_tokens", warm)
            self.assertEqual(real["max_output_tokens"],
                             profile.max_output_tokens)
        warm = llm.request_kwargs(self.sol, UTTS, prewarm=True)
        self.assertEqual(warm["tool_choice"], "none")
        real = llm.request_kwargs(self.sol, UTTS, TAIL, tool_choice="none")
        self.assertEqual(real["tool_choice"], "none")
        self.assertNotIn("tool_choice",
                         llm.request_kwargs(self.sol, UTTS, TAIL))
        self.assertNotIn("tools", llm.request_kwargs(self.luna, UTTS, TAIL))
        self.assertNotIn("tool_choice", llm.request_kwargs(
            self.luna, UTTS, TAIL, tool_choice="auto"))

    def test_service_tier_and_verbosity(self):
        cfg = dict(CONFIG, service_tier="auto", fast_verbosity=None)
        kwargs = llm.request_kwargs(llm.luna_profile(cfg, ""), UTTS, TAIL)
        self.assertNotIn("service_tier", kwargs)
        self.assertNotIn("text", kwargs)

    def test_effort_override(self):
        kwargs = llm.request_kwargs(self.luna, UTTS, TAIL,
                                    effort_override="high",
                                    max_output_tokens=200)
        self.assertEqual(kwargs["input"][-1], {
            "type": "configuration_update", "reasoning": {"effort": "high"}})
        self.assertEqual(kwargs["reasoning"], {"effort": "none"})
        self.assertEqual(kwargs["max_output_tokens"], 200)
        same = llm.request_kwargs(self.luna, UTTS, TAIL,
                                  effort_override="none")
        self.assertEqual(same["input"][-1], TAIL[-1])
        warm = llm.request_kwargs(self.luna, UTTS, effort_override="high",
                                  prewarm=True)
        self.assertEqual(len(warm["input"]), 2)


class PlainKwargsTests(unittest.TestCase):
    def test_plain(self):
        luna = llm.luna_profile(CONFIG, "")
        kwargs = llm.request_kwargs(luna, UTTS[:2], TAIL,
                                    effort_override="high")
        plain = llm.plain_kwargs(kwargs)
        self.assertEqual(plain["instructions"], "SYS\n\nFMT")
        self.assertEqual(plain["reasoning"], {"effort": "high"})
        self.assertEqual(plain["service_tier"], "fast")
        self.assertEqual(plain["max_output_tokens"], 2000)
        for key in ("prompt_cache_options", "text"):
            self.assertNotIn(key, plain)
        self.assertIsInstance(plain["input"], str)
        self.assertTrue(plain["input"].startswith(prompts.TRANSCRIPT_HEADER))
        self.assertIn(UTTS[1] + "\n\nMODE\n\nventana", plain["input"])
        self.assertNotIn("configuration_update", plain["input"])

    def test_plain_keeps_tools(self):
        sol = llm.sol_profile(CONFIG, "")
        plain = llm.plain_kwargs(llm.request_kwargs(
            sol, UTTS, TAIL, tool_choice="auto"))
        self.assertEqual(plain["tools"], [{"type": "web_search"}])
        self.assertEqual(plain["tool_choice"], "auto")


class ResultTests(unittest.TestCase):
    def test_estimate(self):
        self.assertEqual(llm.estimate_tokens("a" * 320), 100)

    def test_metrics_label(self):
        result = llm.CallResult(total=1.44, ttft=0.62, input_tokens=1000,
                                cached_tokens=970)
        self.assertEqual(result.metrics_label(),
                         "1.4s · 1er token 0.6s · caché 97%")
        self.assertEqual(llm.CallResult(total=2.0).metrics_label(), "2.0s")
        self.assertEqual(llm.CallResult().metrics_label(), "")


@mock.patch("llm.log_call")
class StreamTests(unittest.TestCase):
    def test_deltas_usage(self, _log):
        stream = FakeStream([
            event("response.output_text.delta", delta="Hola"),
            event("response.output_text.delta", delta=" mundo"),
            completed(),
        ])
        client = FakeClient(stream)
        seen = []
        result = llm.stream(client, {"model": "m"}, on_delta=seen.append,
                            kind="fast")
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "Hola mundo")
        self.assertEqual(seen, ["Hola", "Hola mundo"])
        self.assertIsNotNone(result.ttft)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.input_tokens, 1000)
        self.assertEqual(result.cached_tokens, 970)
        self.assertEqual(result.cache_write_tokens, 30)
        self.assertEqual(result.output_tokens, 50)
        self.assertTrue(stream.closed)
        self.assertTrue(client.calls[0]["stream"])
        _log.assert_called_once()

    def test_web_status(self, _log):
        stream = FakeStream([
            event("response.web_search_call.searching"),
            event("response.web_search_call.completed"),
            event("response.output_text.delta", delta="x"),
            completed(),
        ])
        statuses = []
        llm.stream(FakeClient(stream), {}, on_status=statuses.append)
        self.assertEqual(statuses,
                         ["buscando en la web…", "leyendo resultados…"])

    def test_cancel(self, _log):
        cancel = threading.Event()

        def deltas():
            yield event("response.output_text.delta", delta="a")
            cancel.set()
            yield event("response.output_text.delta", delta="b")

        stream = FakeStream([])
        stream.__class__ = type("S", (FakeStream,), {
            "__iter__": lambda self: deltas()})
        result = llm.stream(FakeClient(stream), {}, cancel=cancel)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "cancelada")
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.text, "a")
        self.assertTrue(stream.closed)

    def test_retry_plain_on_cache_error(self, _log):
        luna = llm.luna_profile(CONFIG, "")
        kwargs = llm.request_kwargs(luna, UTTS, TAIL)
        good = FakeStream([event("response.output_text.delta", delta="ok"),
                           completed()])
        client = FakeClient(bad_request("bad prompt_cache_options"), good)
        result = llm.stream(client, kwargs)
        self.assertTrue(result.ok)
        self.assertEqual(len(client.calls), 2)
        self.assertIn("instructions", client.calls[1])
        self.assertNotIn("prompt_cache_options", client.calls[1])
        self.assertIn("reasoning", client.calls[1])

    def test_retry_without_tier_and_reasoning(self, _log):
        luna = llm.luna_profile(CONFIG, "")
        kwargs = llm.request_kwargs(luna, UTTS, TAIL)
        good = FakeStream([event("response.output_text.delta", delta="ok"),
                           completed()])
        client = FakeClient(bad_request("unsupported service_tier"), good)
        result = llm.stream(client, kwargs)
        self.assertTrue(result.ok)
        retry = client.calls[1]
        self.assertNotIn("service_tier", retry)
        self.assertNotIn("reasoning", retry)
        self.assertIn("instructions", retry)

    def test_retry_only_once(self, _log):
        client = FakeClient(bad_request("prompt_cache bad"),
                            bad_request("prompt_cache bad again"))
        luna = llm.luna_profile(CONFIG, "")
        result = llm.stream(client, llm.request_kwargs(luna, UTTS, TAIL))
        self.assertFalse(result.ok)
        self.assertEqual(len(client.calls), 2)

    def test_unrelated_bad_request_not_retried(self, _log):
        client = FakeClient(bad_request("context too long"))
        luna = llm.luna_profile(CONFIG, "")
        result = llm.stream(client, llm.request_kwargs(luna, UTTS, TAIL))
        self.assertFalse(result.ok)
        self.assertEqual(len(client.calls), 1)
        self.assertIn("context too long", result.error)

    def test_error_after_deltas_keeps_partial(self, _log):
        stream = FakeStream(
            [event("response.output_text.delta", delta="parcial")],
            raises=RuntimeError("conexión rota"))
        result = llm.stream(FakeClient(stream), {})
        self.assertFalse(result.ok)
        self.assertEqual(result.text, "parcial")
        self.assertIn("conexión rota", result.error)

    def test_failed_event_and_empty(self, _log):
        failed = event("response.failed", response=NS(
            status="failed", usage=None, error=NS(message="boom")))
        result = llm.stream(FakeClient(FakeStream([failed])), {})
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "boom")
        empty = llm.stream(FakeClient(FakeStream([completed()])), {})
        self.assertFalse(empty.ok)
        self.assertEqual(empty.error, "sin texto")


class PrewarmTests(unittest.TestCase):
    @mock.patch("llm.log_call")
    @mock.patch("llm.get_client")
    def test_prewarm(self, get_client, _log):
        response = NS(status="completed", usage=NS(
            input_tokens=900, output_tokens=0,
            input_tokens_details=NS(cached_tokens=0, cache_write_tokens=900)))
        client = FakeClient(response)
        get_client.return_value = client
        sol = llm.sol_profile(CONFIG, "")
        result = llm.prewarm("key", sol, UTTS)
        self.assertTrue(result.ok)
        self.assertEqual(result.kind, "prewarm")
        self.assertEqual(result.cache_write_tokens, 900)
        self.assertTrue(client.calls[0]["prompt_cache_options"]["prewarm"])
        self.assertNotIn("stream", client.calls[0])

    @mock.patch("llm.log_call")
    @mock.patch("llm.get_client")
    def test_prewarm_never_raises(self, get_client, _log):
        get_client.return_value = FakeClient(RuntimeError("nope"))
        result = llm.prewarm("k", llm.luna_profile(CONFIG, ""), UTTS)
        self.assertFalse(result.ok)
        self.assertIn("nope", result.error)


if __name__ == "__main__":
    unittest.main()


class PrewarmTierTests(unittest.TestCase):
    """Los pre-cacheos no corren prisa: por defecto van sin Fast mode (sin
    recargo) y las llamadas reales siguen en Fast."""

    def _config(self, **extra):
        from api_client import DEFAULT_GPT_CONFIG
        cfg = dict(DEFAULT_GPT_CONFIG)
        cfg.update(extra)
        return cfg

    def test_prewarm_drops_fast_tier_by_default(self):
        cfg = self._config()
        for profile in (llm.luna_profile(cfg, ""), llm.sol_profile(cfg, "")):
            warm = llm.request_kwargs(profile, ["Entrevistador: hola"],
                                      prewarm=True)
            real = llm.request_kwargs(profile, ["Entrevistador: hola"])
            self.assertNotIn("service_tier", warm, profile.name)
            self.assertEqual(real.get("service_tier"), "fast", profile.name)
            self.assertTrue(warm["prompt_cache_options"]["prewarm"])

    def test_prewarm_tier_configurable(self):
        cfg = self._config(prewarm_service_tier="fast")
        warm = llm.request_kwargs(llm.luna_profile(cfg, ""), ["a"],
                                  prewarm=True)
        self.assertEqual(warm["service_tier"], "fast")
        cfg = self._config(prewarm_service_tier="flex")
        warm = llm.request_kwargs(llm.sol_profile(cfg, ""), ["a"],
                                  prewarm=True)
        self.assertEqual(warm["service_tier"], "flex")

    def test_prewarm_and_real_share_prefix_regardless_of_tier(self):
        cfg = self._config()
        profile = llm.luna_profile(cfg, "brief")
        lines = [f"Entrevistador: línea {i}" for i in range(5)]
        warm = llm.request_kwargs(profile, lines, prewarm=True)
        real = llm.request_kwargs(profile, lines + ["Tú: más"],
                                  [{"role": "user", "content": "cola"}])
        self.assertEqual(warm["input"][0], real["input"][0])
        self.assertEqual(warm["input"][1]["content"][:6],
                         real["input"][1]["content"][:6])
