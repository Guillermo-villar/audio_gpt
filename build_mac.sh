#!/bin/bash
# build_mac.sh — genera audio_gpt.app y audio_gpt-<versión>-arm64.dmg
# Uso:  ./build_mac.sh           (en un Mac con Python 3.11+ y Homebrew)
set -euo pipefail
cd "$(dirname "$0")"

VERSION="${VERSION:-0.1.0}"
ARCH="$(uname -m)"              # arm64 o x86_64
PYTHON="${PYTHON:-python3}"
VENV=".venv-build"
APP="audio_gpt.app"
DMG="audio_gpt-${VERSION}-${ARCH}.dmg"

echo "== 1/4  Entorno virtual"
"$PYTHON" -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --upgrade pip >/dev/null
pip install -r requirements.txt pyinstaller >/dev/null

echo "== 2/4  PyInstaller -> $APP"
rm -rf build dist
pyinstaller --noconfirm --clean audio_gpt.spec >/dev/null
test -d "dist/$APP"

echo "== 3/4  Firma ad-hoc (sin cuenta de desarrollador)"
# Firma ad-hoc: quita el aviso «está dañada» de Gatekeeper en la mayoría
# de los casos; el primer arranque sigue necesitando «Abrir igualmente».
codesign --force --deep --sign - "dist/$APP" 2>/dev/null || true

echo "== 4/4  DMG -> dist/$DMG"
rm -rf dist/dmgroot "dist/$DMG"
mkdir -p dist/dmgroot
cp -R "dist/$APP" dist/dmgroot/
ln -sf /Applications dist/dmgroot/Applications
hdiutil create -volname "audio_gpt" -srcfolder dist/dmgroot \
    -ov -format UDZO "dist/$DMG" >/dev/null
rm -rf dist/dmgroot

echo
echo "Listo:"
echo "  App: dist/$APP"
echo "  DMG: dist/$DMG"
