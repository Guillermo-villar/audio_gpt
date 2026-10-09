import os
os.environ.setdefault("AUDIO_GPT_NO_LOG", "1")

import unittest

from api_client import needs_web_search


class NeedsWebSearchTests(unittest.TestCase):
    def test_substring_lookalikes_do_not_trigger(self):
        for text in ("how would you handle websocket fan-out",
                     "what about concurrent writes",
                     "design a currency conversion service",
                     "API versioning strategy",
                     "build a website for the shop",
                     "doing research on the dataset"):
            self.assertFalse(needs_web_search(text), text)

    def test_real_hints_trigger(self):
        for text in ("what's the latest Kafka version",
                     "current pricing of S3",
                     "qué hay de nuevo en 2026",
                     "busca la documentación",
                     "who won the match",
                     "look up the docs",
                     "Dime las últimas noticias",
                     "search the web for it"):
            self.assertTrue(needs_web_search(text), text)

    def test_year_rule_needs_whole_number(self):
        self.assertTrue(needs_web_search("roadmap for 2027"))
        self.assertFalse(needs_web_search("handle 120000 requests"))
        self.assertFalse(needs_web_search("id 20245"))

    def test_empty(self):
        self.assertFalse(needs_web_search(""))
        self.assertFalse(needs_web_search(None))


if __name__ == "__main__":
    unittest.main()
