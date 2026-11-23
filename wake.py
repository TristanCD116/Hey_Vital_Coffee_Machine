# "Hey Vital" wake word: openWakeWord's mel -> embedding -> hey_vital.onnx pipeline on onnxruntime (models/).
import os

import numpy as np
import onnxruntime as ort

CHUNK = 1280            # 80 ms at 16 kHz: one embedding per chunk
MEL_KEEP = 970          # ~10 s of mel frames
FEAT_KEEP = 120         # ~10 s of embeddings
WARMUP_CHUNKS = 5       # openWakeWord ignores the first 5 scores after a reset


def _session(path):
    opts = ort.SessionOptions()
    opts.inter_op_num_threads = 1
    opts.intra_op_num_threads = 1
    return ort.InferenceSession(path, sess_options=opts, providers=["CPUExecutionProvider"])


class WakeWord:
    def __init__(self, model_path, models_dir, threshold=0.5, patience=1):
        for f in (model_path, os.path.join(models_dir, "melspectrogram.onnx"),
                  os.path.join(models_dir, "embedding_model.onnx")):
            if not os.path.exists(f):
                raise FileNotFoundError(f"wake word file missing: {f}")
        self.mel = _session(os.path.join(models_dir, "melspectrogram.onnx"))
        self.emb = _session(os.path.join(models_dir, "embedding_model.onnx"))
        self.ww = _session(model_path)
        self.ww_input = self.ww.get_inputs()[0].name
        self.n_frames = int(self.ww.get_inputs()[0].shape[1])      # 16 for hey_vital.onnx
        self.threshold, self.patience = threshold, max(1, int(patience))
        self.reset()

    def reset(self):
        # Clear all audio history so Vital's own voice can't trigger it.
        self.raw = np.zeros(0, dtype=np.int16)
        self.pending = 0
        self.mel_buf = np.ones((76, 32), dtype=np.float32)
        silence = self._embed(np.ones((76, 32), dtype=np.float32))
        self.feat = np.tile(silence, (FEAT_KEEP, 1))
        self.chunks_seen, self.above = 0, 0
        self.last_score = 0.0

    def _melspec(self, audio):
        out = self.mel.run(None, {"input": audio.astype(np.float32)[None, :]})[0]
        return np.squeeze(out) / 10 + 2

    def _embed(self, mel76):
        return self.emb.run(None, {"input_1": mel76[None, :, :, None].astype(np.float32)})[0].squeeze()

    def feed(self, pcm16k):
        # Add 16 kHz int16 audio; True when "Hey Vital" is heard.
        self.raw = np.concatenate([self.raw, pcm16k.astype(np.int16)])[-16000 * 10:]
        self.pending += len(pcm16k)
        if self.pending < CHUNK:
            return False
        n_chunks = self.pending // CHUNK
        self.mel_buf = np.vstack([self.mel_buf, self._melspec(self.raw[-(self.pending + 480):])])
        self.mel_buf = self.mel_buf[-MEL_KEEP:]
        self.pending = 0
        fired = False
        for i in range(n_chunks - 1, -1, -1):
            end = len(self.mel_buf) - 8 * i
            win = self.mel_buf[end - 76:end]
            if len(win) == 76:
                self.feat = np.vstack([self.feat, self._embed(win)])[-FEAT_KEEP:]
            score = float(self.ww.run(None, {self.ww_input: self.feat[-self.n_frames:][None].astype(np.float32)})[0]
                          .squeeze())
            self.chunks_seen += 1
            if self.chunks_seen <= WARMUP_CHUNKS:
                score = 0.0
            self.last_score = score
            self.above = self.above + 1 if score >= self.threshold else 0
            if self.above >= self.patience:
                fired = True
        return fired
