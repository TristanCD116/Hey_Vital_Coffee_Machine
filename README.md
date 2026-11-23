# Vital

Vital is an award-winning smart coffee dispenser that helps you decide how much caffeine to have in the morning. It briefs you using your Google Health and Calendar data, asks four questions, and recommends a caffeine dose. An Arduino Uno then controls the pumps that dispense caffeinated coffee, decaf, and water.

Say **“Hey Vital”**, press **Enter**, or place a cup on the sensor to start.

# Achievements

🥇 The project won 1st place in University of Toronto Engineering Kompetition (UTEK), in the Innovation category.\
☕️ The team advanced to Ontario Engineering Competition 2027, representing UofT provincially; where the team will iterate on the project.

## Photos and videos

Visit the [media folder](media/) for project photos and videos, including the [demo video](media/demo_video_compressed.mp4).

## Documents and Visuals from UTEK
Visit the [documents folder](Docs/) for the team's winning poster, presentation, and design documentation. Most of the information is summarized in [poster](Docs/Hey_Vital_UTEK_Poster.pdf)

## Setup and commands

- **Raspberry Pi** — Run `bash setup_pi.sh`, then `./venv/bin/python vital.py`.
- **Windows** — Run `py -m pip install -r requirements.txt`, then `py vital.py`.

Use `--no-uno` to print values without running the pumps, or `--no-cup` to ignore the cup sensor. Settings are in `config.json`.

| File | Purpose |
| --- | --- |
| `vital.py` | Main loop, prefetch, and morning sequence |
| `dose.py` | Dose formula, limits, and caffeinated/decaf split |
| `chat.py` | Questions, answer parsing, and Gemini |
| `voice.py` | ElevenLabs speech input and output, microphone, and speaker |
| `wake.py` | “Hey Vital” wake word detection using `models/` |
| `google_data.py` | Google Health and Calendar |
| `uno.py` | Pumps and cup sensor over USB |

## Model attribution

`models/melspectrogram.onnx` and `models/embedding_model.onnx` are included unmodified from release v0.5.1 of [openWakeWord](https://github.com/dscripka/openWakeWord) by David Scripka. They are licensed under [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/).
