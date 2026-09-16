#!/data/data/com.termux/files/usr/bin/python3
# -*- coding: utf-8 -*-
"""
本地 TTS HTTP 服务（全离线、多音色、流式）

给 TTS Server 的 JS 插件调用：插件返回一个 http:// 地址，App 自己来取音频。

为什么要子进程隔离：
    sherpa-onnx 的 ZipVoice 在「极短文本 + speed 偏高」时会直接 segfault
    （实测：2 个字 + speed1.2 必崩，4 个字就没事）。
    如果合成跑在服务进程里，一次崩溃整个服务就没了，必须手动重启。
    所以合成放在常驻子进程里，崩了父进程自动拉起，只影响当次请求。

启动：
    python3 tts_server.py                    # 默认 127.0.0.1:8765
    python3 tts_server.py --host 0.0.0.0     # 局域网可访问

接口：
    GET /tts?text=你好&format=mp3&speed=1.0
    GET /health
"""

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import wave
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def ensure_pcm16(path, rate=24000):
    """把任意音频转成 24kHz 单声道 16-bit PCM wav，并缓存。

    参考音频格式完全不可控——可能是 mp3、24-bit wav、立体声、任意采样率
    （常见的无损 wav 就可能长这样）。
    与其要求用户自己转，不如服务端统一转掉。
    """
    try:
        st = os.stat(path)
        sig = "%s|%d|%d|%d" % (os.path.abspath(path), st.st_mtime_ns, st.st_size, rate)
    except OSError:
        sig = "%s|%d" % (os.path.abspath(path), rate)
    h = hashlib.md5(sig.encode("utf-8")).hexdigest()[:16]

    cache_dir = os.path.join(tempfile.gettempdir(), "tts_ref_cache")
    os.makedirs(cache_dir, exist_ok=True)
    out = os.path.join(cache_dir, h + ".wav")
    if os.path.exists(out) and os.path.getsize(out) > 1024:
        return out

    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", path,
         "-ac", "1", "-ar", str(rate), "-c:a", "pcm_s16le", out],
        check=True)
    return out

SAFE_SPEED_MIN = 0.5
SAFE_SPEED_MAX = 1.2
# 实测崩溃点：文本很短且 speed 接近上限。这里主动躲开。
SHORT_TEXT_LIMIT = 4
SHORT_TEXT_SPEED = 1.1

# 流式模式下每段最多多少字。切得越细首段越快，但段间停顿也越多。
STREAM_CHUNK_CHARS = 60

# 音色库目录：voices/<角色名>/ 下一个文件夹就是一个音色
VOICES_DIR = os.path.join(HERE, "voices")


# ================================================================ 音色库（目录即音色）

def scan_voices():
    """扫描 voices/<角色>/，一个文件夹 = 一个音色。

    约定（放在文件夹里的文件）：
        ref.wav     参考音频，必需（也可以放别的 .wav，取第一个）
        ref.txt     参考音频里逐字准确的内容，必需
        meta.json   可选：{"id": ..., "name": ..., "icon": ...}
                    id 不写就用文件夹名，name 不写就用文件夹名

    每次请求都重新扫，所以**新增角色不用重启服务**。
    """
    out = []
    if not os.path.isdir(VOICES_DIR):
        return out
    for name in sorted(os.listdir(VOICES_DIR)):
        d = os.path.join(VOICES_DIR, name)
        if not os.path.isdir(d) or name.startswith("."):
            continue

        wav = os.path.join(d, "ref.wav")
        if not os.path.isfile(wav):
            cands = sorted(f for f in os.listdir(d) if f.lower().endswith(".wav"))
            if not cands:
                continue
            wav = os.path.join(d, cands[0])

        txt_path = os.path.join(d, "ref.txt")
        if not os.path.isfile(txt_path):
            continue
        try:
            with open(txt_path, encoding="utf-8") as f:
                ref_text = f.read().strip()
        except OSError:
            continue
        if not ref_text:
            continue

        meta = {}
        mp = os.path.join(d, "meta.json")
        if os.path.isfile(mp):
            try:
                with open(mp, encoding="utf-8") as f:
                    meta = json.load(f)
            except (OSError, ValueError):
                meta = {}

        out.append({
            "id": str(meta.get("id") or name),
            "name": str(meta.get("name") or name),
            "icon": str(meta.get("icon") or "female"),
            "dir": name,
            "ref": wav,
            "ref_text": ref_text,
        })
    return out


def resolve_voice(voice_id, fallback_ref=None, fallback_text=None):
    """按 id 找音色。找不到时退回默认参考音频。"""
    vs = scan_voices()
    if voice_id:
        for v in vs:
            if v["id"] == voice_id or v["dir"] == voice_id or v["name"] == voice_id:
                return v
    if vs:
        return vs[0]
    if fallback_ref:
        return {"id": "default", "name": "默认", "icon": "female", "dir": "default",
                "ref": fallback_ref, "ref_text": fallback_text or ""}
    return None


# ================================================================ 子进程：真正合成

def worker_main(args):
    import sherpa_onnx
    from clone_zh import build_tts, read_wav, clean_text

    os.chdir(HERE)
    tts = build_tts(num_threads=args.threads)

    # 参考音频按需加载并缓存（key = 参考音频路径 + 文本）
    ref_cache = {}

    def get_ref(ref_path, ref_text):
        key = (ref_path, ref_text)
        if key not in ref_cache:
            # 任何格式的参考音频都先统一成 24kHz 单声道 16-bit
            audio, rate = read_wav(ensure_pcm16(ref_path))
            ref_cache[key] = (audio, rate, clean_text(ref_text))
            # 只留最近 4 个，避免换很多角色后占内存
            if len(ref_cache) > 4:
                ref_cache.pop(next(iter(ref_cache)))
        return ref_cache[key]

    # 必须走 stdout：父进程在 stdout 上等这一行
    print(json.dumps({"ok": True, "ready": True}), flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if line == "__ping__":
            print(json.dumps({"ok": True, "pong": True}), flush=True)
            continue
        try:
            req = json.loads(line)
            text = clean_text(req["text"])
            speed = float(req.get("speed", 1.0))
            out = req["out"]
            ref_path = req.get("ref") or getattr(args, "ref", None)
            if not ref_path:
                print(json.dumps({"ok": False, "err": "没有可用音色：voices/ 为空"}), flush=True)
                continue
            ref, sr, ref_text = get_ref(ref_path, req.get("ref_text") or args.ref_text or "")
            mcs = req.get("min_char_in_sentence", 30)

            # 整段一次生成。
            # 不要按句切开分别 generate 再 concat —— 每次 generate 都是独立随机采样，
            # 段与段之间音色会变（听感：前半句一个声音、后半句换成另一个声音）。
            gen = sherpa_onnx.GenerationConfig()
            gen.reference_audio = ref
            gen.reference_sample_rate = sr
            gen.reference_text = ref_text
            gen.num_steps = 4
            gen.speed = speed
            gen.extra["min_char_in_sentence"] = str(mcs)
            audio = tts.generate(text, gen)
            if len(audio.samples) == 0:
                print(json.dumps({"ok": False, "err": "模型没有产出音频"}), flush=True)
                continue
            rate = audio.sample_rate
            samples = np.asarray(audio.samples, dtype=np.float32)
            pcm = np.clip(samples * 32767.0, -32767.0, 32767.0).astype(np.int16)
            with wave.open(out, "wb") as w:
                w.setframerate(rate)
                w.setsampwidth(2)
                w.setnchannels(1)
                w.writeframes(pcm.tobytes())
            print(json.dumps({"ok": True, "rate": rate, "samples": int(len(samples))}), flush=True)
        except (Exception, SystemExit) as e:  # noqa: BLE001
            print(json.dumps({"ok": False, "err": str(e)}), flush=True)


_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])")
_SUB_SPLIT = re.compile(r"(?<=[，,、：:])")


def split_sentences(text, limit=50):
    """切句。先按句末标点切，太长再按逗号切，还太长就硬切——
    流式播放时切得越细，第一段音频来得越快。"""
    out = []
    for piece in _SENT_SPLIT.split(text):
        if not piece:
            continue
        if len(piece) <= limit:
            out.append(piece)
            continue
        cur = ""
        for sub in _SUB_SPLIT.split(piece):
            if len(cur) + len(sub) > limit and cur:
                out.append(cur)
                cur = sub
            else:
                cur += sub
        if cur:
            out.append(cur)
    # 兜底：没有任何标点的超长串
    final = []
    for p in out:
        while len(p) > limit * 2:
            final.append(p[:limit])
            p = p[limit:]
        if p:
            final.append(p)
    return final or [text]


# ================================================================ 父进程：worker 管理

class Worker:
    """常驻合成子进程；崩溃自动重启，只让当次请求失败。"""

    def __init__(self, args):
        self.args = args
        self.proc = None
        self.lock = threading.Lock()
        self.restarts = 0

    def start(self):
        cmd = [sys.executable, os.path.abspath(__file__), "--worker",
               "--threads", str(self.args.threads)]
        if getattr(self.args, "ref", None):
            cmd += ["--ref", self.args.ref, "--ref-text", self.args.ref_text or ""]
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, cwd=HERE,
        )
        # 等它加载完模型
        line = self.proc.stdout.readline()
        return bool(line)

    def request(self, text, speed, out, ref=None, ref_text=None, min_char_in_sentence=30):
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                if self.proc is not None:
                    self.restarts += 1
                if not self.start():
                    raise RuntimeError("合成进程启动失败")
            payload = {"text": text, "speed": speed, "out": out,
                       "min_char_in_sentence": min_char_in_sentence}
            if ref:
                payload["ref"] = ref
            if ref_text is not None:
                payload["ref_text"] = ref_text
            try:
                self.proc.stdin.write(json.dumps(payload) + "\n")
                self.proc.stdin.flush()
                line = self.proc.stdout.readline()
            except Exception as e:  # noqa: BLE001
                line = ""
                err = str(e)
            else:
                err = ""

            if not line:
                # 子进程死了（多半是 segfault）
                try:
                    self.proc.kill()
                except Exception:  # noqa: BLE001
                    pass
                self.proc = None
                self.restarts += 1
                self.start()
                raise RuntimeError("合成进程崩溃已自动重启（%s）" % (err or "segfault"))

            resp = json.loads(line)
            if not resp.get("ok"):
                raise RuntimeError(resp.get("err", "未知错误"))
            return resp

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


# ================================================================ HTTP

WORKER = None
STATS = {"ok": 0, "fail": 0}
MP3_CACHE = {}
DEFAULT_REF = [None, None]   # 找不到 voice 参数时用的兜底参考音频 [路径, 文本]


def wav_to_mp3(wav_path, streaming=False):
    """wav -> mp3。streaming=True 时去掉 ID3/Xing 头，
    这样多段 mp3 直接首尾相接就是一个连续可播的流。"""
    dst = wav_path[:-4] + (".s.mp3" if streaming else ".mp3")
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", wav_path]
    if streaming:
        cmd += ["-write_xing", "0", "-id3v2_version", "0"]
    cmd += ["-codec:a", "libmp3lame", "-b:a", "64k", dst]
    subprocess.run(cmd, check=True)
    return dst


_SILENCE_MP3 = None


def silence_mp3(seconds=0.12, rate=24000):
    """句间停顿。只生成一次，之后复用。"""
    global _SILENCE_MP3
    if _SILENCE_MP3 is None:
        tmp = tempfile.mktemp(suffix=".wav")
        pcm = np.zeros(int(rate * seconds), dtype=np.int16)
        with wave.open(tmp, "wb") as w:
            w.setframerate(rate)
            w.setsampwidth(2)
            w.setnchannels(1)
            w.writeframes(pcm.tobytes())
        p = wav_to_mp3(tmp, streaming=True)
        with open(p, "rb") as f:
            _SILENCE_MP3 = f.read()
        for q in (tmp, p):
            if os.path.exists(q):
                os.remove(q)
    return _SILENCE_MP3


def clamp_speed(text, speed):
    if speed < SAFE_SPEED_MIN:
        speed = SAFE_SPEED_MIN
    elif speed > SAFE_SPEED_MAX:
        speed = SAFE_SPEED_MAX
    # 规避已知崩溃点
    if len(text) < SHORT_TEXT_LIMIT and speed > SHORT_TEXT_SPEED:
        speed = SHORT_TEXT_SPEED
    return speed


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        print("[%s] %s" % (time.strftime("%H:%M:%S"), fmt % a), flush=True)

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- 流式（chunked）----

    def _begin_chunked(self, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _write_chunk(self, data):
        if not data:
            return
        self.wfile.write(b"%x\r\n" % len(data))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()          # 关键：立刻推出去，不等攒完

    def _end_chunked(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _stream_tts(self, text, speed, fmt, voice):
        """边合成边发：每合成完一句就编码成 mp3 推给客户端。"""
        pieces = split_sentences(text, limit=STREAM_CHUNK_CHARS)
        self._begin_chunked("audio/wav" if fmt == "wav" else "audio/mpeg")
        t0 = time.time()
        first_at = None
        total_audio = 0.0
        sent = 0
        try:
            for i, piece in enumerate(pieces):
                tmp = tempfile.mktemp(suffix=".wav")
                try:
                    resp = WORKER.request(piece, speed, tmp, voice["ref"], voice["ref_text"])
                    total_audio += resp["samples"] / resp["rate"]
                    if fmt == "wav":
                        # wav 不能像 mp3 那样简单拼接，整段退回非流式更稳妥
                        with open(tmp, "rb") as f:
                            self._write_chunk(f.read())
                    else:
                        if i > 0:
                            self._write_chunk(silence_mp3())
                        mp3 = wav_to_mp3(tmp, streaming=True)
                        with open(mp3, "rb") as f:
                            self._write_chunk(f.read())
                        os.remove(mp3)
                    if first_at is None:
                        first_at = time.time() - t0
                    sent += 1
                finally:
                    if os.path.exists(tmp):
                        os.remove(tmp)
            self._end_chunked()
        except Exception as e:  # noqa: BLE001
            STATS["fail"] += 1
            print("  流式中断（已发出的 %d 句仍可播）: %s" % (sent, e), flush=True)
            try:
                self._end_chunked()
            except Exception:  # noqa: BLE001
                pass
            return

        STATS["ok"] += 1
        el = time.time() - t0
        print("  [流式][%s] %d 字 -> %d 段 / %.2fs 音频, 总耗时 %.2fs, 首段 %.2fs (RTF %.2f)"
              % (voice["name"], len(text), sent, total_audio, el, first_at or 0,
                 el / total_audio if total_audio else 0), flush=True)

    def do_GET(self):
        from clone_zh import clean_text

        u = urlparse(self.path)
        q = parse_qs(u.query)

        # 音色列表：插件启动/刷新时拉这个 —— 所以加角色不用改插件
        if u.path == "/voices":
            vs = scan_voices()
            body = json.dumps(
                [{"id": v["id"], "name": v["name"], "icon": v["icon"]} for v in vs],
                ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return

        if u.path == "/health":
            alive = WORKER.proc is not None and WORKER.proc.poll() is None
            vs = scan_voices()
            body = json.dumps({"ok": True, "worker": alive, "restarts": WORKER.restarts,
                               "synth_ok": STATS["ok"], "synth_fail": STATS["fail"],
                               "voices": [v["id"] for v in vs]}).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return

        if u.path not in ("/tts", "/"):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return

        text = clean_text((q.get("text") or q.get("t") or [""])[0])
        fmt = (q.get("format") or q.get("f") or ["mp3"])[0].lower()
        voice_id = (q.get("voice") or q.get("v") or [""])[0]
        try:
            speed = float((q.get("speed") or ["1.0"])[0])
        except ValueError:
            speed = 1.0
        speed = clamp_speed(text, speed)

        if not text:
            self._send(400, b"empty text", "text/plain; charset=utf-8")
            return

        voice = resolve_voice(voice_id, DEFAULT_REF[0], DEFAULT_REF[1])
        if voice is None:
            self._send(500, b"no voice available (voices/ is empty)", "text/plain; charset=utf-8")
            return

        mcs = q.get("min_char_in_sentence") or q.get("mcs")
        mcs = int(mcs[0]) if mcs else 30
        stream = (q.get("stream") or q.get("s") or ["0"])[0].lower() in ("1", "true", "yes", "on")
        if stream:
            self._stream_tts(text, speed, fmt, voice)
            return

        t0 = time.time()
        tmp = tempfile.mktemp(suffix=".wav")
        try:
            resp = WORKER.request(text, speed, tmp, voice["ref"], voice["ref_text"], mcs)
        except Exception as e:  # noqa: BLE001
            STATS["fail"] += 1
            print("  合成失败: %s" % e, flush=True)
            self._send(503, ("synth failed: %s" % e).encode("utf-8"), "text/plain; charset=utf-8")
            return

        dur = resp["samples"] / resp["rate"]
        try:
            if fmt == "wav":
                with open(tmp, "rb") as f:
                    body, ctype = f.read(), "audio/wav"
            else:
                mp3 = wav_to_mp3(tmp)
                with open(mp3, "rb") as f:
                    body, ctype = f.read(), "audio/mpeg"
        finally:
            for p in (tmp, tmp[:-4] + ".mp3"):
                if os.path.exists(p):
                    os.remove(p)

        STATS["ok"] += 1
        print("  [%s] %d 字 -> %.2fs 音频, 耗时 %.2fs (RTF %.2f, speed=%.2f)"
              % (voice["name"], len(text), dur, time.time() - t0,
                 (time.time() - t0) / dur, speed), flush=True)
        self._send(200, body, ctype)


def main():
    global WORKER

    ap = argparse.ArgumentParser(description="本地语音引擎（多音色）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--ref", default=None,
                    help="兜底参考音频（可选）。正常情况下都用 voices/ 里的音色，不用这个")
    ap.add_argument("--ref-text", default="", help="兜底参考音频对应的文本")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.worker:
        worker_main(args)
        return

    os.chdir(HERE)
    if args.ref:
        DEFAULT_REF[0] = os.path.join(HERE, args.ref) if not os.path.isabs(args.ref) else args.ref
        DEFAULT_REF[1] = args.ref_text

    vs = scan_voices()
    print("音色库 %s：找到 %d 个音色" % (VOICES_DIR, len(vs)), flush=True)
    for v in vs:
        print("   - %-12s %s" % (v["id"], v["name"]), flush=True)
    if not vs:
        print("   （空）还没有任何音色，请先用 make_voice.py 建一个", flush=True)

    print("启动合成子进程（加载模型约 2 秒）...", flush=True)
    WORKER = Worker(args)
    if not WORKER.start():
        sys.exit("合成子进程启动失败")

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print()
    print("服务已启动: http://%s:%d" % (args.host, args.port), flush=True)
    print("  音色列表: curl -s http://127.0.0.1:%d/voices" % args.port, flush=True)
    print("  测试: curl -o t.mp3 'http://127.0.0.1:%d/tts?text=你好'" % args.port, flush=True)
    print("  新增音色: 在 voices/ 下新建文件夹放 ref.wav + ref.txt，不用重启", flush=True)
    print("  停止: Ctrl+C", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        WORKER.stop()
        print("\n已停止。成功 %d 次，失败 %d 次，子进程重启 %d 次"
              % (STATS["ok"], STATS["fail"], WORKER.restarts))


if __name__ == "__main__":
    main()
