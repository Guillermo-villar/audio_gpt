"""Punto de entrada: python main.py

AUDIO_GPT_MOCK=1 arranca con el backend simulado (sin red ni gasto de
tokens): ver mock_llm.py.
"""

import os

if os.environ.get("AUDIO_GPT_MOCK"):
    import mock_llm
    mock_llm.install()

from gui import main  # noqa: E402

if __name__ == "__main__":
    main()
