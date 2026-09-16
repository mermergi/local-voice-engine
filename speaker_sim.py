#!/data/data/com.termux/files/usr/bin/python3
# -*- coding: utf-8 -*-
"""
音色相似度检查：判断克隆出来的声音「像不像」参考音色。

用 CAM++ 说话人声纹模型取 embedding，算余弦相似度。
必须先做对照：同人应高分，不同人应低分——尺子不成立就不能拿来下结论。

用法：
    python3 speaker_sim.py --ref my_voice.wav --test a.wav b.wav ...
"""

import argparse
import os
import subprocess
import sys
import tempfile
import wave

import numpy as np
import sherpa_onnx

SV_MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sv", "campplus_meta.onnx")


def read_16k(path):
    sr = ch = None
    try:
        with wave.open(path, "rb") as w:
            sr, ch = w.getframerate(), w.getnchannels()
    except wave.Error:
        pass  # mp3 等非 wav，交给 ffmpeg
    src = path
    if sr != 16000 or ch != 1:
        tmp = os.path.join(tempfile.gettempdir(), "sv16k_%d.wav" % os.getpid())
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", path, "-ac", "1", "-ar", "16000", tmp], check=True)
        src = tmp
    with wave.open(src, "rb") as w:
        d = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    return np.ascontiguousarray(d)


def build():
    cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
        model=SV_MODEL, num_threads=4, debug=False, provider="cpu"
    )
    if not cfg.validate():
        raise SystemExit("声纹模型加载失败（可能不是 sherpa-onnx 导出的格式）")
    return sherpa_onnx.SpeakerEmbeddingExtractor(cfg)


def embed(ex, path):
    s = ex.create_stream()
    s.accept_waveform(16000, read_16k(path))
    s.input_finished()
    v = np.array(ex.compute(s), dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", required=True, help="参考音频（目标音色）")
    ap.add_argument("--test", nargs="+", required=True, help="要比对的音频")
    ap.add_argument("--neg", nargs="*", default=[], help="已知不同人的音频（对照用）")
    args = ap.parse_args()

    ex = build()
    r = embed(ex, args.ref)
    print("参考音色: %s" % args.ref)
    print()
    print("%-38s %s" % ("音频", "余弦相似度"))
    for p in args.test:
        print("%-38s %.4f" % (os.path.basename(p), float(np.dot(r, embed(ex, p)))))
    if args.neg:
        print("\n--- 负例对照（不同说话人，应明显更低）---")
        for p in args.neg:
            print("%-38s %.4f" % (os.path.basename(p), float(np.dot(r, embed(ex, p)))))


if __name__ == "__main__":
    main()
