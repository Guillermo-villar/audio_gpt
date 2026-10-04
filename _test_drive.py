"""Temporary test driver (NOT part of the PR).

Runs the real WhisperApp with TWO substitutions forced by this VM having
zero audio endpoints and no paid API key:

1. ContinuousCaptureThread._blocks yields looped SAPI-TTS speech
   (48 kHz stereo float32) instead of reading a microphone/loopback.
   Everything downstream — VadSegmenter, wav segments, worker queue,
   transcribe_file, error signals — is the real production code.

2. A QTimer presets state a successful recording would produce
   (current_audio_file + enabled buttons + transcript text) so the
   manual "Transcribir grabación" / "Enviar a GPT" buttons can be
   exercised with real clicks.
"""

import os
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import soundfile as sf
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer

import gui
import vad
import transcriber


# Canned transcription: the VM has no paid API key, so provider calls can only
# 401 (already proven live). Returning text keeps the pipeline RUNNING so we
# can test stop/close lifecycle. Everything else (VAD, queue, worker, signals,
# _on_new_segment) is still the real production code path.
def _canned_transcribe(api_key, file_path, language=None, provider="openai",
                       model="", prompt=None):
    return f"Texto transcrito de {os.path.basename(file_path)}"


transcriber.transcribe_file = _canned_transcribe


# ---------- fake capture blocks: looped speech + silence gap ----------
_speech, _sr = sf.read("speech_en.wav")
_speech = np.asarray(_speech, dtype=np.float32)
if _speech.ndim > 1:
    _speech = _speech.mean(axis=1)
_s48 = vad.resample_linear(_speech, _sr, 48000)
_gap = np.zeros(int(1.2 * 48000), dtype=np.float32)   # silence between utterances
_loop = np.concatenate([_s48, _gap])


def fake_blocks(self):
    block = int(self.samplerate * 0.1)
    i = 0
    while self.running:
        chunk = _loop[i:i + block]
        if len(chunk) < block:
            chunk = np.concatenate([chunk, _loop[:block - len(chunk)]])
        i = (i + block) % len(_loop)
        yield np.column_stack([chunk, chunk])


gui.ContinuousCaptureThread._blocks = fake_blocks


def main():
    app = QApplication(sys.argv)
    win = gui.WhisperApp()
    win.show()

    def preset():
        # what a successful manual recording would set up
        path = os.path.abspath("recording.wav")
        sf.write(path, _speech, 16000)
        win.current_audio_file = path
        win.play_button.setEnabled(True)
        win.transcribe_button.setEnabled(True)
        win.transcription_output.setPlainText(
            "¿Qué es un transformer en machine learning y para qué sirve?"
        )

    # auto-GPT probe: once the checkbox is on, inject one segment through the
    # real _on_new_segment slot -> real GptQueryThread -> status bar shows 401
    def autogpt_probe():
        if win.auto_gpt_checkbox.isChecked():
            win._on_new_segment("¿Qué es un transformer en machine learning?")
        else:
            QTimer.singleShot(3000, autogpt_probe)

    QTimer.singleShot(1500, preset)
    QTimer.singleShot(30000, autogpt_probe)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
