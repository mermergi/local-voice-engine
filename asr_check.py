#!/data/data/com.termux/files/usr/bin/python3
# -*- coding: utf-8 -*-
"""
SenseVoice 语音转写（用来闭环验证克隆出来的语音到底说了什么）

用法：
    python3 asr_check.py a.wav [b.wav ...]
"""

import os
import subprocess
import sys
import tempfile
import wave

import numpy as np
import sherpa_onnx

ASR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "asr")


def read_wav_16k(path):
    """读成 16kHz 单声道 float32；不是 wav / 不是 16k 就先用 ffmpeg 转一次。"""
    sr = ch = None
    try:
        with wave.open(path, "rb") as w:
            sr, ch = w.getframerate(), w.getnchannels()
    except wave.Error:
        pass  # mp3 等非 wav，直接交给 ffmpeg
    src = path
    if sr != 16000 or ch != 1:
        tmp = os.path.join(tempfile.gettempdir(), "asr_16k_%d.wav" % os.getpid())
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", path, "-ac", "1", "-ar", "16000", tmp],
            check=True,
        )
        src = tmp
    with wave.open(src, "rb") as w:
        n = w.getnframes()
        data = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
    return np.ascontiguousarray(data), 16000


def build():
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=os.path.join(ASR_DIR, "model.int8.onnx"),
        tokens=os.path.join(ASR_DIR, "tokens.txt"),
        use_itn=True,
        language="zh",
        debug=False,
        num_threads=4,
        provider="cpu",
    )


def main():
    files = sys.argv[1:]
    if not files:
        sys.exit("用法: asr_check.py <wav> [...]")
    rec = build()
    for f in files:
        samples, sr = read_wav_16k(f)
        s = rec.create_stream()
        s.accept_waveform(sr, samples)
        rec.decode_stream(s)
        print("%-34s %s" % (os.path.basename(f), s.result.text))


if __name__ == "__main__":
    main()
