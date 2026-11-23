#!/bin/bash
# One-time Pi setup (64-bit Raspberry Pi OS): system audio library, Python packages, wake-word models.
set -e
cd "$(dirname "$0")"
sudo apt-get update && sudo apt-get install -y python3-venv python3-numpy libportaudio2 wget
python3 -m venv --system-site-packages venv && ./venv/bin/pip install -r requirements.txt
R=https://github.com/dscripka/openWakeWord/releases/download/v0.5.1
for m in melspectrogram embedding_model; do [ -f models/$m.onnx ] || wget -q -P models "$R/$m.onnx"; done
echo "Done. Run: ./venv/bin/python vital.py"
