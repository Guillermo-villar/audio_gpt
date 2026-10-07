import os
os.environ.setdefault("AUDIO_GPT_NO_LOG", "1")
import os
import types
import unittest
from types import SimpleNamespace as NS
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import api_client
import copilot
import gui
import prompts

CONFIG = dict(api_client.DEFAULT_GPT_CONFIG)


class Flag:
    def __init__(self, checked=True):
        self.checked = checked

    def isChecked(self):
        return self.checked


class Timer:
    def __init__(self):
        self.started = None
        self.stopped = False

    def start(self, ms):
        self.started = ms

    def stop(self):
        self.stopped = True


def fake_app(engine="openai", prewarm=True, continuous=True):
    app = NS(
        gpt_engine_combo=NS(currentData=lambda: engine),
        prewarm_checkbox=Flag(prewarm),
        is_continuous_mode=continuous,
        _prewarm=copilot.PrewarmScheduler(),
        _prewarm_errors={},
        _prewarm_warned=set(),
        _prewarm_cfg=dict(CONFIG),
        _prewarm_key="",
        _prewarm_timer=Timer(),
        _ping_timer=Timer(),
    )
    app._prewarm_enabled = types.MethodType(
        gui.WhisperApp._prewarm_enabled, app)
    app._ping_tick = types.MethodType(gui.WhisperApp._ping_tick, app)
    return app


class PingDecoupleTests(unittest.TestCase):
    def start(self, app, key="k"):
        with mock.patch.object(gui.ApiKeyManager, "load_api_key",
                               return_value=key), \
                mock.patch.object(gui.GptClient, "load_config",
                                  return_value=dict(CONFIG)), \
                mock.patch.object(gui.threading, "Thread") as thread:
            gui.WhisperApp._start_prewarm(app)
            return thread

    def test_ping_runs_with_prewarm_off(self):
        app = fake_app(prewarm=False)
        thread = self.start(app)
        self.assertEqual(app._ping_timer.started, 30000)
        self.assertIsNone(app._prewarm_timer.started)
        thread.assert_called_once()
        self.assertEqual(thread.call_args.kwargs["args"],
                         ("k", CONFIG["model"]))

    def test_prewarm_on_starts_both(self):
        app = fake_app(prewarm=True)
        self.start(app)
        self.assertEqual(app._ping_timer.started, 30000)
        self.assertEqual(app._prewarm_timer.started, 400)

    def test_no_key_nothing_runs(self):
        app = fake_app()
        thread = self.start(app, key="")
        self.assertIsNone(app._ping_timer.started)
        self.assertIsNone(app._prewarm_timer.started)
        thread.assert_not_called()

    def test_codex_engine_skips_ping(self):
        app = fake_app(engine="codex")
        app._prewarm_key = "k"
        with mock.patch.object(gui.threading, "Thread") as thread:
            app._ping_tick()
        thread.assert_not_called()

    def test_cloudflare_pings_detail_model(self):
        app = fake_app(engine="cloudflare")
        app._prewarm_key = "k"
        with mock.patch.object(gui.threading, "Thread") as thread:
            app._ping_tick()
        self.assertEqual(thread.call_args.kwargs["args"],
                         ("k", CONFIG["detail_model"]))

    def test_not_continuous_skips(self):
        app = fake_app(continuous=False)
        app._prewarm_key = "k"
        with mock.patch.object(gui.threading, "Thread") as thread:
            app._ping_tick()
        thread.assert_not_called()


class DiagramConfigTests(unittest.TestCase):
    def test_config_keys(self):
        self.assertEqual(CONFIG["diagram_prompt"], prompts.DIAGRAM_MODE)
        self.assertEqual(CONFIG["diagram_reasoning_effort"], "low")
        self.assertEqual(CONFIG["diagram_max_tokens"], 1500)

    def test_hotkey_registered(self):
        mod, key, name, label = gui.HOTKEYS["alt+w"]
        self.assertEqual((mod, key, name), (0x12, 0x57, "draw"))
        self.assertTrue(label.startswith("Alt+W"))
        self.assertTrue(hasattr(gui.WhisperApp, "_hk_draw"))
        self.assertTrue(hasattr(gui.WhisperApp, "_draw_architecture"))
        combos = [(m, k) for m, k, _, _ in gui.HOTKEYS.values()]
        self.assertEqual(len(combos), len(set(combos)))

    def test_no_automatic_answering(self):
        self.assertNotIn("ctrl+m", gui.HOTKEYS)
        for name in ("_fire_gpt", "_gate_async", "_on_gate_fired",
                     "_hk_toggle_auto", "_review_effort",
                     "_on_manual_only_changed"):
            self.assertFalse(hasattr(gui.WhisperApp, name), name)
        self.assertFalse(hasattr(gui, "clef_question"))
        self.assertFalse(hasattr(gui, "looks_like_question"))
        self.assertFalse(hasattr(api_client, "clef_question"))
        self.assertFalse(hasattr(prompts, "CLEF_INSTRUCTIONS"))
        self.assertFalse(hasattr(prompts, "SD_GATE_HINTS"))

    def test_diagram_tail(self):
        window = ["Entrevistador: dibuja", "Tú: vale"]
        tail = copilot.diagram_tail(CONFIG, window, [])
        self.assertEqual(tail[0], {
            "role": "developer", "content": prompts.DIAGRAM_MODE})
        self.assertEqual(
            tail[1]["content"],
            prompts.WINDOW_HEADER + "\n" + "\n".join(window))
        pinned = copilot.diagram_tail(CONFIG, window, ["QPS 12k"])
        self.assertTrue(pinned[1]["content"].startswith(
            prompts.PINNED_HEADER + "\n- QPS 12k\n\n"))


if __name__ == "__main__":
    unittest.main()
