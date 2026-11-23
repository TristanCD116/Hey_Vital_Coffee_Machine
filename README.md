# Vital

Say "Hey Vital" (or press Enter, or place a cup): Vital briefs you from Google Health + Calendar, asks four questions, recommends a caffeine dose and pumps caf, decaf and water from the Uno.

**Pi:** `bash setup\_pi.sh`, then `./venv/bin/python vital.py`
**PC:** `py -m pip install -r requirements.txt`, then `py vital.py`

Options: `--no-uno` (print values only), `--no-cup` (ignore the cup sensor). Settings are in `config.json`.

|`vital.py`|Main loop, prefetch, the morning sequence|
|-|-|
|`dose.py`|Dose formula, limits, caf/decaf split|
|`chat.py`|Questions, answer parsing, Gemini|
|`voice.py`|ElevenLabs speech in/out, mic and speaker|
|`wake.py`|"Hey Vital" wake word (`models/`)|
|`google\_data.py`|Google Health and Calendar|
|`uno.py`|Pumps and cup sensor over USB|

models/melspectrogram.onnx and models/embedding\_model.onnx are from openWakeWord

by David Scripka (https://github.com/dscripka/openWakeWord), release v0.5.1,

licensed under CC BY-NC-SA 4.0 (https://creativecommons.org/licenses/by-nc-sa/4.0/).

They are included unmodified.

