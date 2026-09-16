#!/data/data/com.termux/files/usr/bin/python3
# -*- coding: utf-8 -*-
"""
下载运行需要的模型。

    python3 download_models.py              # 只下合成必需的（约 185 MB）
    python3 download_models.py --verify     # 连带验证用的 ASR + 声纹模型（约 450 MB）
    python3 download_models.py --hf-mirror  # 用 hf-mirror.com 代替 huggingface.co
    python3 download_models.py --gh-proxy https://ghfast.top
                                            # github 直连不通时挂个代理前缀

下完的东西都在模型自己的许可下分发，本仓库不含任何模型文件。
用量与出处见 README。
"""

import argparse
import hashlib
import os
import shutil
import sys
import ssl
import tarfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

ZIPVOICE_TAR = "sherpa-onnx-zipvoice-distill-int8-zh-en-emilia.tar.bz2"
ZIPVOICE_DIR = "sherpa-onnx-zipvoice-distill-int8-zh-en-emilia"
# 可选：fp32 版。更大更慢，音质差异作者没能客观测出来，见 README「模型选择」
ZIPVOICE_FP32_TAR = "sherpa-onnx-zipvoice-distill-fp32-zh-en-emilia.tar.bz2"
ZIPVOICE_FP32_DIR = "sherpa-onnx-zipvoice-distill-fp32-zh-en-emilia"

GH = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
HF = "https://huggingface.co"

TTS_TARGETS = [
    # (目标路径, 下载地址)
    (ZIPVOICE_TAR, GH + "/tts-models/" + ZIPVOICE_TAR),
    ("vocos_24khz.onnx", GH + "/vocoder-models/vocos_24khz.onnx"),
]

VERIFY_TARGETS = [
    ("asr/model.int8.onnx",
     HF + "/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/model.int8.onnx"),
    ("asr/tokens.txt",
     HF + "/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/tokens.txt"),
    ("sv/campplus.onnx",
     HF + "/welcomyou/campplus-3dspeaker-200k-onnx/resolve/main/campplus_cn_en_common_200k.onnx"),
]

UA = {"User-Agent": "Mozilla/5.0 (local-voice-engine model fetcher)"}


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return "%.1f %s" % (n, u)
        n /= 1024.0


def fetch(url, dest, proxy=None, ctx=None):
    """下载到 dest，已存在且非空则跳过。支持断点续传。"""
    if os.path.exists(dest) and os.path.getsize(dest) > 1024:
        print("  已存在，跳过  %s (%s)" % (dest, human(os.path.getsize(dest))))
        return True

    full = (proxy.rstrip("/") + "/" + url) if proxy else url
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    part = dest + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0

    req = urllib.request.Request(full, headers=dict(UA))
    if have:
        req.add_header("Range", "bytes=%d-" % have)

    try:
        with urllib.request.urlopen(req, timeout=60, context=ctx) as r, \
                open(part, "ab" if have else "wb") as f:
            total = int(r.headers.get("Content-Length") or 0) + have
            done = have
            while True:
                chunk = r.read(262144)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    sys.stdout.write("\r  %-46s %5.1f%%  %s / %s"
                                     % (os.path.basename(dest), done * 100.0 / total,
                                        human(done), human(total)))
                    sys.stdout.flush()
        sys.stdout.write("\n")
    except Exception as e:  # noqa: BLE001
        print("\n  下载失败: %s\n    %s" % (e, full))
        return False

    os.replace(part, dest)
    print("  完成 %s (%s)" % (dest, human(os.path.getsize(dest))))
    return True


def extract_zipvoice(tar_name, dir_name):
    tar = os.path.join(HERE, tar_name)
    if os.path.isdir(os.path.join(HERE, dir_name)):
        return True
    if not os.path.exists(tar):
        print("  找不到 %s" % tar)
        return False
    print("  解压 %s ..." % tar_name)
    with tarfile.open(tar, "r:bz2") as t:
        t.extractall(HERE)
    os.remove(tar)
    print("  解压完成，已删除压缩包")
    return True


def add_onnx_metadata(path, meta):
    """给 ONNX 文件补 metadata（sherpa-onnx 的声纹模型需要它才知道模型类型）。

    这里不依赖 onnx 包（很多平台上装不上），直接按 protobuf 线格式
    往文件尾部追加 ModelProto.metadata_props（字段号 14）。
    metadata_props 是 repeated 字段，追加是合法的；已有张量数据在文件前部，
    偏移不受影响。
    """
    def varint(n):
        out = bytearray()
        while True:
            b = n & 0x7F
            n >>= 7
            out.append(b | 0x80 if n else b)
            if not n:
                return bytes(out)

    def ld(field, payload):
        return varint((field << 3) | 2) + varint(len(payload)) + payload

    def entry(k, v):
        return ld(1, k.encode()) + ld(2, str(v).encode())

    try:
        have = open(path, "rb").read()
    except OSError as e:
        print("  读不了 %s: %s" % (path, e))
        return False
    # 粗查一下有没有已经写过（避免重复追加）
    if b"3d-speaker" in have[-4096:]:
        print("  已有 metadata，跳过")
        return True

    blob = b"".join(varint((14 << 3) | 2) + varint(len(entry(k, v))) + entry(k, v)
                    for k, v in meta.items())
    with open(path, "ab") as f:
        f.write(blob)
    print("  已补 metadata: %s" % ", ".join(meta))
    return True


def main():
    ap = argparse.ArgumentParser(description="下载本地语音引擎需要的模型")
    ap.add_argument("--verify", action="store_true",
                    help="额外下载 ASR 与声纹模型（asr_check.py / speaker_sim.py 用）")
    ap.add_argument("--hf-mirror", action="store_true",
                    help="huggingface.co 不通时，改用 hf-mirror.com")
    ap.add_argument("--gh-proxy", default="",
                    help="github 不通时填代理前缀，如 https://ghfast.top")
    ap.add_argument("--fp32", action="store_true",
                    help="额外下载 fp32 模型（更大更慢；音质差异作者未能客观测出）")
    ap.add_argument("--insecure", action="store_true",
                    help="关闭 HTTPS 证书校验。有些网络（含部分代理）会做中间人，"
                         "报 CERTIFICATE_VERIFY_FAILED 时可用；确认网络可信再用")
    args = ap.parse_args()

    os.chdir(HERE)
    hf_host = "https://hf-mirror.com" if args.hf_mirror else HF

    ctx = None
    if args.insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        print("⚠ --insecure：已关闭证书校验（只在你确认网络可信时用）\n")

    ok = True
    print("== 合成必需 ==")
    already = os.path.isdir(os.path.join(HERE, ZIPVOICE_DIR))
    for dest, url in TTS_TARGETS:
        if dest == ZIPVOICE_TAR and already:
            print("  已解压，跳过  %s/（不再下载压缩包）" % ZIPVOICE_DIR)
            continue
        u = url.replace(HF, hf_host) if url.startswith(HF) else url
        proxy = args.gh_proxy if u.startswith("https://github.com") else ""
        if not fetch(u, dest, proxy, ctx):
            ok = False
    if ok:
        ok = extract_zipvoice(ZIPVOICE_TAR, ZIPVOICE_DIR)

    if args.fp32:
        print("\n== 可选：fp32 模型（约 478 MB，慢 2.5 倍）==")
        if os.path.isdir(os.path.join(HERE, ZIPVOICE_FP32_DIR)):
            print("  已解压，跳过  %s/" % ZIPVOICE_FP32_DIR)
        else:
            u = GH + "/tts-models/" + ZIPVOICE_FP32_TAR
            if fetch(u, ZIPVOICE_FP32_TAR, args.gh_proxy, ctx):
                ok = extract_zipvoice(ZIPVOICE_FP32_TAR, ZIPVOICE_FP32_DIR) and ok
            else:
                ok = False

    if args.verify:
        print("\n== 验证工具（可选）==")
        for dest, url in VERIFY_TARGETS:
            u = url.replace(HF, hf_host)
            proxy = args.gh_proxy if u.startswith("https://github.com") else ""
            if not fetch(u, dest, proxy, ctx):
                ok = False
        sv = os.path.join(HERE, "sv", "campplus.onnx")
        if os.path.exists(sv):
            print("  声纹模型需要补 metadata（sherpa-onnx 靠它识别模型类型）")
            add_onnx_metadata(sv, {
                "framework": "3d-speaker",
                "language": "Chinese",
                "url": "https://huggingface.co/welcomyou/campplus-3dspeaker-200k-onnx",
                "comment": "metadata appended by download_models.py",
                "sample_rate": "16000",
                "output_dim": "192",
                "normalize_samples": "1",
            })
            # speaker_sim.py 读的是带 metadata 的那份
            shutil.copy(sv, os.path.join(HERE, "sv", "campplus_meta.onnx"))

    print()
    if not ok:
        sys.exit("有文件没下成功，请看上面的报错")
    print("模型就绪。下一步：")
    print("  1) python3 make_voice.py --wav 你的参考音频.wav --text \"里面说的话\" --name \"音色名\"")
    print("  2) bash start.sh")
    print("  3) curl -s http://127.0.0.1:8765/voices")


if __name__ == "__main__":
    main()
