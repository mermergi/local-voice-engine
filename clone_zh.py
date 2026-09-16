#!/data/data/com.termux/files/usr/bin/python3
# -*- coding: utf-8 -*-
"""
零样本语音合成内核（ZipVoice + sherpa-onnx，全离线 / 纯 CPU）

不需要训练。给一段参考音频 + 它的准确文字，就能用这个音色合成任意文本。

依赖：sherpa-onnx（有原生 android arm64 轮子）、ZipVoice 模型、vocos 声码器
用法：
    python3 clone_zh.py --ref my_voice.wav \
        --ref-text "这段录音里说的话" \
        --text "要合成的文本" -o out.wav
"""

import argparse
import os
import re
import time
import wave

import numpy as np
import sherpa_onnx

MODEL_DIR = "sherpa-onnx-zipvoice-distill-int8-zh-en-emilia"

# 很多转录文本里混着不少「不是字」的符号。实测：把它们当字喂给参考文本，
# 模型会对齐错，合成结果开头冒出「跳达」「秋打」这类杂音。
# 例：参考文本末尾多余的 '~' 会让模型对不齐，去掉就干净了。
_NOISE_CHARS = "~～…—♪"          # 拖音/省略/破折/音符
_QUOTES = "“”\"'‘’「」『』《》"
_BRACKET_NOTE = re.compile(r"[（(][^）)]*[）)]")   # （建立羁绊）这类注释


def clean_text(s, drop_brackets=False):
    """把文本清理成「模型能对齐的、真的被念出来的字」。"""
    s = s or ""
    if drop_brackets:
        s = _BRACKET_NOTE.sub("", s)
    for ch in _NOISE_CHARS:
        s = s.replace(ch, "")
    for ch in _QUOTES:
        s = s.replace(ch, "")
    s = re.sub(r"\s+", "", s)
    return s.strip()


def read_wav(path):
    """读 16-bit PCM wav -> (float32 mono, sample_rate)。不依赖 soundfile。"""
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2:
            raise SystemExit("只支持 16-bit PCM wav: %s" % path)
        n, ch, sr = w.getnframes(), w.getnchannels(), w.getframerate()
        data = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
        if ch > 1:
            data = data.reshape(-1, ch)[:, 0]
        return np.ascontiguousarray(data), sr


def write_wav(path, samples, sample_rate):
    pcm = np.clip(np.asarray(samples, dtype=np.float32) * 32767.0, -32767.0, 32767.0).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setframerate(sample_rate)
        w.setsampwidth(2)
        w.setnchannels(1)
        w.writeframes(pcm.tobytes())


def build_tts(num_threads=4, debug=False):
    cfg = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            zipvoice=sherpa_onnx.OfflineTtsZipvoiceModelConfig(
                tokens=os.path.join(MODEL_DIR, "tokens.txt"),
                encoder=os.path.join(MODEL_DIR, "encoder.int8.onnx"),
                decoder=os.path.join(MODEL_DIR, "decoder.int8.onnx"),
                data_dir=os.path.join(MODEL_DIR, "espeak-ng-data"),
                lexicon=os.path.join(MODEL_DIR, "lexicon.txt"),
                vocoder="vocos_24khz.onnx",
            ),
            debug=debug,
            num_threads=num_threads,
            provider="cpu",
        )
    )
    if not cfg.validate():
        raise SystemExit("配置校验失败，请看上面的报错")
    return sherpa_onnx.OfflineTts(cfg)


def main():
    ap = argparse.ArgumentParser(description="ZipVoice 零样本语音克隆（离线 CPU）")
    ap.add_argument("--ref", required=True, help="参考音频 wav（建议 24kHz 单声道，3-10 秒）")
    ap.add_argument("--ref-text", required=True, help="参考音频里逐字准确的内容")
    ap.add_argument("--text", required=True, help="要合成的文本")
    ap.add_argument("-o", "--out", default="cloned.wav")
    ap.add_argument("--steps", type=int, default=4, help="流匹配步数（默认 4）")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--min-char", default="30", help="extra['min_char_in_sentence']")
    ap.add_argument("--raw-text", action="store_true", help="不做文本清理，照原样喂")
    ap.add_argument("--drop-brackets", action="store_true", help="额外去掉（建立羁绊）这类注释")
    args = ap.parse_args()

    ref, sr = read_wav(args.ref)
    ref_text = args.ref_text if args.raw_text else clean_text(args.ref_text, args.drop_brackets)
    text = args.text if args.raw_text else clean_text(args.text, args.drop_brackets)

    print("参考音频 %s: %.2f 秒 @ %d Hz" % (os.path.basename(args.ref), len(ref) / sr, sr))
    print("参考文本: %s%s" % (ref_text, "" if args.raw_text else "   (已清理)"))
    print("目标文本: %s%s" % (text, "" if args.raw_text else "   (已清理)"))

    t0 = time.time()
    tts = build_tts(num_threads=args.threads)
    print("模型加载: %.1f 秒" % (time.time() - t0))

    gen = sherpa_onnx.GenerationConfig()
    gen.reference_audio = ref
    gen.reference_sample_rate = sr
    gen.reference_text = ref_text
    gen.num_steps = args.steps
    gen.speed = args.speed
    gen.extra["min_char_in_sentence"] = args.min_char

    t0 = time.time()
    audio = tts.generate(text, gen)
    dt = time.time() - t0

    if len(audio.samples) == 0:
        raise SystemExit("生成失败，请看上面的报错")

    dur = len(audio.samples) / audio.sample_rate
    write_wav(args.out, audio.samples, audio.sample_rate)
    print("已生成 %s" % args.out)
    print("  采样率 %d Hz | 时长 %.2f 秒" % (audio.sample_rate, dur))
    print("  合成耗时 %.2f 秒 | RTF = %.3f （<1 表示比实时快）" % (dt, dt / dur if dur else 0))


if __name__ == "__main__":
    main()
