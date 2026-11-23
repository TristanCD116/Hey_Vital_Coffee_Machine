# Cloud voice: ElevenLabs listens (Scribe realtime) and speaks (Flash); sounddevice runs the mic and speaker.
import base64
import hashlib
import json
import os
import queue
import sys
import threading
import time
import wave

import numpy as np
import requests
import sounddevice as sd

HERE = os.path.dirname(os.path.abspath(__file__))
RATE, OUT_RATE = 16000, 22050  # mic audio for the cloud and wake word; ElevenLabs pcm_22050 output


class Downsampler:
    # Streaming mic-rate -> 16 kHz with a low-pass first, so mic hiss doesn't alias into the wake word's band.
    def __init__(self, rate_in, taps=63):
        self.step, cutoff = rate_in / RATE, 0.45 * RATE / rate_in
        n = np.arange(taps) - (taps - 1) / 2
        h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hamming(taps)
        self.h, self.hist, self.buf, self.pos = (h / h.sum()).astype(np.float32), np.zeros(taps - 1, np.float32), np.zeros(0, np.float32), 0.0

    def __call__(self, x):
        x = x.astype(np.float32)
        if self.step == 1:
            return x
        xx = np.concatenate([self.hist, x])
        self.hist, self.buf = xx[-(len(self.h) - 1):], np.concatenate([self.buf, np.convolve(xx, self.h, mode="valid")])
        idx = np.arange(self.pos, len(self.buf) - 1, self.step)
        out, nxt = np.interp(idx, np.arange(len(self.buf)), self.buf), (idx[-1] + self.step if len(idx) else self.pos)
        self.buf, self.pos = self.buf[int(nxt):], nxt - int(nxt)
        return out


def eleven_key():
    p = os.path.join(HERE, "elevenlabs_key.txt")
    return os.environ.get("ELEVENLABS_API_KEY") or (open(p).read().strip() if os.path.exists(p) else None)


class CloudListener:
    # One realtime WebSocket per answer; audio recorded before it connects is queued and sent.
    URL = ("wss://api.elevenlabs.io/v1/speech-to-text/realtime?model_id=scribe_v2_realtime"
           "&audio_format=pcm_16000&language_code=en&commit_strategy=manual")

    def __init__(self, key, context=""):
        self.key, self.context, self.ws, self.pending, self.first, self.idle, self.created = key, context, None, [], True, True, time.time()
        self.partial, self.committed, self.error, self.lock, self.done = "", [], None, threading.Lock(), threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _msg(self, pcm, commit=False):
        m = {"message_type": "input_audio_chunk", "audio_base_64": base64.b64encode(pcm).decode(), "commit": commit, "sample_rate": RATE}
        if self.first and self.context:
            m["previous_text"] = self.context[:500]  # the question helps it hear the answer
        self.first = False
        return json.dumps(m)

    def _keepalive(self):
        # Opened while the question plays: a little silence keeps it open until the answer starts.
        while self.idle and self.error is None and time.time() - self.created < 20:
            try:
                with self.lock:
                    if self.idle:
                        self.ws.send(self._msg(b"\x00\x00" * 1600))
            except Exception as e:
                self.error = str(e)
            time.sleep(0.5)

    def _run(self):
        try:
            import websocket
            ws = websocket.create_connection(self.URL, header=[f"xi-api-key: {self.key}"], timeout=5)
            ws.settimeout(None)
            with self.lock:
                for pcm in self.pending:
                    ws.send(self._msg(pcm))
                self.pending, self.ws = [], ws
            threading.Thread(target=self._keepalive, daemon=True).start()
            while True:
                m = json.loads(ws.recv())
                t = m.get("message_type")
                if t == "partial_transcript":
                    self.partial = m.get("text", "")
                elif t == "committed_transcript":
                    self.committed.append(m.get("text", ""))
                    self.done.set()
                elif t not in ("session_started", "warning") and "error" in m:
                    self.error = f"{t}: {m.get('error')}"
                    return self.done.set()
        except Exception as e:
            if not self.done.is_set():
                self.error = self.error or str(e)
                self.done.set()

    def send(self, pcm):
        with self.lock:
            self.idle = False
            if self.ws is None:
                return self.pending.append(pcm)
            try:
                self.ws.send(self._msg(pcm))
            except Exception as e:
                self.error = str(e)

    def text_so_far(self):
        return " ".join([t for t in self.committed if t] + [self.partial]).strip()

    def finish(self, timeout):
        # Commit and wait for the final text; None if the cloud didn't answer.
        t0 = time.time()
        while self.ws is None and self.error is None and time.time() - t0 < timeout:
            time.sleep(0.05)
        try:
            if self.ws is not None:
                with self.lock:
                    self.ws.send(self._msg(b"\x00\x00" * 1600, commit=True))
            self.done.wait(max(0.1, timeout - (time.time() - t0)))
        finally:
            try:
                self.ws and self.ws.close()
            except Exception:
                pass
        if self.error:
            print(f"  [cloud stt] {self.error}")
            return None
        return " ".join(t for t in self.committed if t).strip() if self.done.is_set() else (self.partial or None)


class Tts:
    # ElevenLabs text to speech; every line is cached in tts_cache/ so it's paid for and waited on once.
    def __init__(self, key, voice_id, model):
        self.voice_id, self.model, self.s, self.dir = voice_id, model, requests.Session(), os.path.join(HERE, "tts_cache")
        self.s.headers["xi-api-key"] = key
        os.makedirs(self.dir, exist_ok=True)

    def path(self, text):
        return os.path.join(self.dir, hashlib.sha1(f"{self.voice_id}|{self.model}|{text}".encode()).hexdigest()[:16] + ".wav")

    def cached(self, text):
        if os.path.exists(self.path(text)):
            with wave.open(self.path(text), "rb") as r:
                return r.readframes(r.getnframes())

    def _save(self, text, frames):
        tmp = self.path(text) + ".tmp"
        with wave.open(tmp, "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(OUT_RATE), w.writeframes(frames)
        os.replace(tmp, self.path(text))

    def _request(self, text, stream):
        r = self.s.post(f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}" + ("/stream" if stream else ""), stream=stream,
                        params={"output_format": "pcm_22050"}, json={"text": text, "model_id": self.model}, timeout=10)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:150]}")
        return r

    def fetch(self, text):
        if self.cached(text) is None:
            self._save(text, self._request(text, False).content)

    def stream(self, text):
        # Yield whole 16-bit samples as they arrive; cache the line at the end.
        got, carry = [], b""
        for chunk in self._request(text, True).iter_content(chunk_size=4410):
            chunk = carry + chunk
            chunk, carry = chunk[:len(chunk) // 2 * 2], chunk[len(chunk) // 2 * 2:]
            if chunk:
                got.append(chunk)
                yield chunk
        self._save(text, b"".join(got))


class Voice:
    def __init__(self, cfg):
        self.a, self.key = cfg["audio"], eleven_key()
        if not self.key:
            raise RuntimeError("no ElevenLabs key: put it in elevenlabs_key.txt")
        self.tts = Tts(self.key, self.a.get("eleven_voice_id", "JBFqnCBsd6RMkjVDRZzb"), self.a.get("eleven_tts_model", "eleven_flash_v2_5"))
        self.out = self._device(self.a.get("output_device", ""), "output")
        self.mic, self.mic_rate = self._find_mic(self.a.get("mic_name", ""))
        self._pre, self._listen_after, self._speech_end, self.last_first_audio = None, False, 0.0, 0.0
        self.beep_start, self.beep_end, self.beep_wake = self._tone([660, 990], 0.07, 0.35), self._tone([1320], 0.04, 0.2), self._tone([523, 784, 1047], 0.09, 0.35)
        print("[voice] listening and speaking: ElevenLabs")

    def _device(self, name, kind):
        # Device index whose name contains `name`, else None (= system default).
        for i, d in enumerate(sd.query_devices() if name else []):
            if d[f"max_{kind}_channels"] > 0 and name.lower() in d["name"].lower() and (
                    kind == "output" or not sys.platform.startswith("win") or sd.query_hostapis(d["hostapi"])["name"] == "MME"):
                return i
        if name:
            print(f"[voice] no {kind} named '{name}', using the default")

    def _find_mic(self, name):
        dev = self._device(name, "input")
        info = sd.query_devices(dev, "input")
        for rate in (RATE, 48000, 44100, int(info["default_samplerate"])):
            try:
                sd.check_input_settings(device=dev, samplerate=rate, channels=1, dtype="int16")
                print(f"[voice] mic: {info['name']} @ {rate} Hz")
                return dev, rate
            except Exception:
                pass
        raise RuntimeError("no usable microphone found")

    def _tone(self, freqs, dur, vol):
        t = np.arange(int(OUT_RATE * dur)) / OUT_RATE
        env = np.minimum(1, np.minimum(t, dur - t) / 0.01)  # soft edges, no clicks
        return b"".join((np.sin(2 * np.pi * f * t) * env * vol * 32767).astype(np.int16).tobytes() for f in freqs)

    def _out(self):
        return sd.RawOutputStream(samplerate=OUT_RATE, channels=1, dtype="int16", device=self.out)

    def _play(self, frames, wait=True):
        def go():
            with self._out() as s:
                s.write(frames)
        go() if wait else threading.Thread(target=go, daemon=True).start()

    def prepare(self, lines):
        # Record fixed lines in the background so they play instantly later.
        def go():
            try:
                for line in lines:
                    self.tts.fetch(line)
            except Exception as e:
                print(f"  [voice] couldn't pre-record lines ({e})")
        threading.Thread(target=go, daemon=True).start()

    def prerender(self, text):
        try:
            self.tts.fetch(text)
            return True
        except Exception as e:
            print(f"  [voice] pre-record failed ({e})")
            return False

    def say(self, text):
        # Play the recorded line, or stream it; the "talk now" beep follows if a question is waiting.
        print(f"VITAL > {text}")
        beep, self._listen_after = self._listen_after, False
        t0, frames, first = time.time(), self.tts.cached(text), None
        with self._out() as s:
            s.write(b"\x00\x00" * int(OUT_RATE * 0.03))
            try:
                for chunk in [frames] if frames is not None else self.tts.stream(text):
                    first = first or time.time()
                    s.write(chunk)
            except Exception as e:
                print(f"  [voice] ElevenLabs failed ({e}); the line above was not spoken")
            if beep:
                s.write(b"\x00\x00" * int(OUT_RATE * 0.04) + self.beep_start)
        self.last_first_audio = first or time.time()
        self._speech_end = time.time()
        print(f"  [time] voice: {'pre-recorded' if frames is not None else 'streamed'}, sound after {self.last_first_audio - t0:.2f} s")
        if not beep:
            time.sleep(self.a.get("pause_after_speaking_s", 0.1))

    def _open_mic(self):
        q = queue.Queue()
        stream = sd.InputStream(device=self.mic, samplerate=self.mic_rate, channels=1, dtype="int16", blocksize=int(self.mic_rate * 0.05),
                                callback=lambda d, *_: q.put((time.time(), d[:, 0].copy())))
        stream.start()
        return q, stream

    def prepare_listen(self, question, kind=None):
        # Open the mic and the cloud listener before the question is spoken; audio during speech is discarded.
        self._close_pre()
        try:
            q, stream = self._open_mic()
            self._pre, self._listen_after = {"kind": kind, "q": q, "stream": stream, "cloud": CloudListener(self.key, question)}, True
        except Exception as e:
            print(f"  [mic] couldn't pre-open ({e})")
            self._pre, self._listen_after = None, False

    def _close_pre(self):
        if self._pre:
            self._pre["stream"].stop(), self._pre["stream"].close()
            self._pre = None

    def listen(self, kind=None, question=""):
        # Stream the answer; stop once the live transcript is a valid answer and you pause, on silence, or at a limit.
        from chat import quick_parse
        a, pre, self._pre = self.a, self._pre, None
        if pre and pre["kind"] == kind:
            q, stream, cloud, cutoff = pre["q"], pre["stream"], pre["cloud"], self._speech_end + a.get("echo_settle_s", 0.3)
        else:
            if pre:
                pre["stream"].stop(), pre["stream"].close()
            self._play(self.beep_start)
            (q, stream), cloud, cutoff = self._open_mic(), CloudListener(self.key, question), 0.0
        levels, started, quiet, loud_run, answer, n, t0, t_start = [], False, 0.0, 0, None, 0, time.time(), None
        try:
            while time.time() - t0 < a["listen_timeout_s"]:
                try:
                    stamp, block = q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if stamp < cutoff:  # still the speaker's echo
                    continue
                x = block.astype(np.float32) * a["mic_gain"]
                if self.mic_rate != RATE:
                    x = np.interp(np.linspace(0, len(x) - 1, int(round(len(x) * RATE / self.mic_rate))), np.arange(len(x)), x)
                pcm, n = np.clip(x, -32768, 32767).astype(np.int16), n + 1
                rms = float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2)))
                levels.append(rms)
                floor = min(float(np.median(levels[:6])), a.get("noise_floor_cap", 250))
                if rms > max(2.5 * floor, a["speech_threshold"]):
                    loud_run += 1
                    if loud_run >= a.get("min_speech_blocks", 3):  # a stray beep can't start or end an answer
                        started, quiet, t_start = True, 0.0, t_start or time.time()
                    elif started:
                        quiet = 0.0
                else:
                    loud_run, quiet = 0, quiet + 0.05 if started else 0.0
                cloud.send(pcm.tobytes())
                if started and kind and n % 2 == 0 and quick_parse(cloud.text_so_far(), kind):
                    answer = cloud.text_so_far()
                if cloud.error or started and (quiet >= (a.get("answer_pause_s", 0.3) if answer else a["end_silence_s"])) or (
                        t_start and time.time() - t_start > a.get("max_answer_s", 4)):
                    break
        finally:
            stream.stop(), stream.close()
        self._play(self.beep_end, wait=False)
        t1 = time.time()
        if answer and not cloud.error:
            threading.Thread(target=cloud.finish, args=(2,), daemon=True).start()
            text, src = answer, "live transcript"
        else:
            text = cloud.finish(a.get("cloud_wait_s", 2.5)) or ""
            src = "cloud failed" if cloud.error else f"cloud +{time.time() - t1:.1f} s"
        text = text.strip().rstrip(".!?")
        print(f"YOU   > {text}   ({time.time() - t0:.1f} s, {src})")
        return text

    def wait_for_wake(self, wake, start):
        # Block until "Hey Vital", Enter, or a cup (start event), then chime.
        wake.reset()
        start.clear()
        down, q = Downsampler(self.mic_rate), queue.Queue()
        gain = self.a.get("wake_gain", self.a["mic_gain"])
        with sd.InputStream(device=self.mic, samplerate=self.mic_rate, channels=1, dtype="int16",
                            blocksize=int(self.mic_rate * 0.08), callback=lambda d, *_: q.put(d[:, 0].copy())):
            while not start.is_set():
                try:
                    block = q.get(timeout=0.2)
                except queue.Empty:
                    continue
                if wake.feed(np.clip(down(block) * gain, -32768, 32767).astype(np.int16)):
                    print(f"[wake] heard \"Hey Vital\" (score {wake.last_score:.2f})")
                    break
        if start.is_set():
            print(f"[start] {start.reason}")
        self._play(self.beep_wake)
