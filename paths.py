"""Dónde escribir datos de la app (settings, claves, temporales).

Con el .app de macOS el bundle es de solo lectura: todo lo escribible va a
`~/Library/Application Support/audio_gpt`. En desarrollo y en Windows se
usa el directorio del proyecto, como siempre.
"""

import os
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))


def is_frozen_mac():
    return getattr(sys, "frozen", False) and sys.platform == "darwin"


def data_dir():
    """Directorio escribible para settings.json, claves, transcripts y
    temporales. En el .app firmado/ad hoc es Application Support."""
    if is_frozen_mac():
        d = os.path.join(os.path.expanduser("~"),
                         "Library", "Application Support", "audio_gpt")
        os.makedirs(d, exist_ok=True)
        return d
    return APP_DIR
