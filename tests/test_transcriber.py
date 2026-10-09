import os
os.environ.setdefault("AUDIO_GPT_NO_LOG", "1")
import json
import unittest
import urllib.parse

import transcriber


def query(url):
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)


class DeepgramUrlTests(unittest.TestCase):
    terms = ["rate limiter", "Kafka"]

    def test_flux_has_keyterms(self):
        rt = transcriber.DeepgramRealtime(
            "k", model="flux-general-multi", keyterms=self.terms)
        url = rt._build_url()
        self.assertTrue(url.startswith(transcriber.DEEPGRAM_FLUX_URL + "?"))
        self.assertEqual(query(url)["keyterm"], self.terms)
        self.assertEqual(query(url)["model"], ["flux-general-multi"])

    def test_nova_has_keyterms(self):
        rt = transcriber.DeepgramRealtime(
            "k", model="nova-3", language="en", keyterms=self.terms)
        url = rt._build_url()
        self.assertTrue(url.startswith(transcriber.DEEPGRAM_URL + "?"))
        self.assertEqual(query(url)["keyterm"], self.terms)
        self.assertEqual(query(url)["language"], ["en"])

    def test_no_keyterms(self):
        for model in ("flux-general-multi", "nova-3"):
            url = transcriber.DeepgramRealtime("k", model=model)._build_url()
            self.assertNotIn("keyterm", query(url))


NOVA_FINAL = {
    "type": "Results", "is_final": True, "start": 2.5, "duration": 3.0,
    "channel": {"alternatives": [{
        "transcript": "how would you scale it",
        "words": [
            {"word": "how", "punctuated_word": "How", "start": 2.5,
             "end": 2.7, "confidence": 0.99},
            {"word": "scale", "start": 3.0, "end": 3.4,
             "confidence": 0.5}]}]},
}
NOVA_INTERIM = dict(NOVA_FINAL, is_final=False)
FLUX_END = {
    "type": "TurnInfo", "event": "EndOfTurn", "audio_window_start": 1.0,
    "audio_window_end": 4.5, "transcript": "design a rate limiter",
    "words": [{"word": "design", "confidence": 0.9},
              {"word": "limiter", "confidence": 0.4}],
}
FLUX_UPDATE = dict(FLUX_END, event="Update")


class ParseEventTests(unittest.TestCase):
    def rt(self, **kw):
        return transcriber.DeepgramRealtime("k", **kw)

    def test_nova_final_meta(self):
        text, final, meta = self.rt(model="nova-3")._parse_event(NOVA_FINAL)
        self.assertEqual(text, "how would you scale it")
        self.assertTrue(final)
        self.assertEqual((meta["start"], meta["end"]), (2.5, 5.5))
        self.assertEqual(meta["words"][0], {
            "word": "How", "start": 2.5, "end": 2.7, "confidence": 0.99})
        self.assertEqual(meta["words"][1]["word"], "scale")

    def test_nova_interim_has_no_meta(self):
        text, final, meta = self.rt(model="nova-3")._parse_event(NOVA_INTERIM)
        self.assertFalse(final)
        self.assertIsNone(meta)

    def test_nova_empty_transcript(self):
        event = {"type": "Results", "is_final": True,
                 "channel": {"alternatives": [{"transcript": ""}]}}
        self.assertIsNone(self.rt()._parse_event(event))

    def test_flux_end_of_turn(self):
        text, final, meta = self.rt()._parse_event(FLUX_END)
        self.assertTrue(final)
        self.assertEqual((meta["start"], meta["end"]), (1.0, 4.5))
        self.assertEqual([w["word"] for w in meta["words"]],
                         ["design", "limiter"])
        self.assertEqual(meta["words"][1]["confidence"], 0.4)
        self.assertIsNone(meta["words"][0]["start"])

    def test_flux_update_is_interim(self):
        text, final, meta = self.rt()._parse_event(FLUX_UPDATE)
        self.assertFalse(final)
        self.assertIsNone(meta)

    def test_other_events(self):
        self.assertIsNone(self.rt()._parse_event({"type": "Metadata"}))
        self.assertIsNone(self.rt()._parse_event({"type": "Error"}))

    def test_diarize_tags_text_but_keeps_meta(self):
        event = json.loads(json.dumps(NOVA_FINAL))
        for i, w in enumerate(event["channel"]["alternatives"][0]["words"]):
            w["speaker"] = 1
        text, final, meta = self.rt(model="nova-3", diarize=True)._parse_event(
            event)
        self.assertIn("<S1>", text)
        self.assertIsNotNone(meta)

    def test_with_meta_callback_arity(self):
        calls = []
        plain = self.rt(on_transcript=lambda *a: calls.append(a))
        plain._emit("t", True, {"m": 1})
        self.assertEqual(calls[-1], ("t", True))
        rich = self.rt(on_transcript=lambda *a: calls.append(a),
                       with_meta=True)
        rich._emit("t", True, {"m": 1})
        self.assertEqual(calls[-1], ("t", True, {"m": 1}))

    def test_recv_loop_uses_meta_callback(self):
        got = []
        rt = self.rt(model="nova-3", with_meta=True,
                     on_transcript=lambda *a: got.append(a))
        messages = [json.dumps(NOVA_INTERIM), json.dumps(NOVA_FINAL), ""]

        class WS:
            def recv(self):
                return messages.pop(0)

        rt._ws = WS()
        rt._running = True
        rt._recv_loop()
        self.assertEqual(len(got), 2)
        self.assertIsNone(got[0][2])
        self.assertEqual(got[1][2]["end"], 5.5)
        self.assertTrue(rt._final_event.is_set())


if __name__ == "__main__":
    unittest.main()
