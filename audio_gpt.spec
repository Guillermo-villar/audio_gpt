# PyInstaller spec para audio_gpt en macOS (Apple Silicon / Intel).
# Uso: pyinstaller audio_gpt.spec
import os

block_cipher = None

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("gpt_config.json", "."),
    ],
    hiddenimports=[
        "capture_mac", "platform_mac", "paths",
        "objc", "Quartz", "AppKit", "Foundation",
        "ScreenCaptureKit", "CoreMedia", "libdispatch",
        "ApplicationServices", "CoreFoundation",
        "sounddevice", "soundfile",
        "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
    ],
    hookspath=["hooks"],
    runtime_hooks=[],
    excludes=["comtypes", "pycaw", "soundcard", "win32com"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="audio_gpt",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,            # app sin terminal
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,         # arch nativo (arm64 en Apple Silicon)
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="audio_gpt",
)
app = BUNDLE(
    coll,
    name="audio_gpt.app",
    icon=None,
    bundle_identifier="com.audio-gpt.app",
    version="0.1.0",
    info_plist={
        "CFBundleName": "audio_gpt",
        "CFBundleDisplayName": "audio_gpt",
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription":
            "audio_gpt usa el micrófono para transcribir tu voz durante "
            "la entrevista.",
        "NSScreenCaptureUsageDescription":
            "audio_gpt graba el audio del sistema (la voz del "
            "entrevistador) con ScreenCaptureKit para transcribirla.",
        "NSAccessibilityUsageDescription":
            "audio_gpt necesita Accesibilidad para los atajos globales "
            "(Ctrl+Q, Ctrl+I, Alt+S…).",
        "LSMinimumSystemVersion": "13.0",
    },
)
