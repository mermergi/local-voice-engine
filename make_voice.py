#!/data/data/com.termux/files/usr/bin/python3
# -*- coding: utf-8 -*-
"""
建一个音色。

音色就是 voices/<名字>/ 下一个文件夹，里面三个文件：
    ref.wav     参考音频（任意格式都行，服务端会自动转码）
    ref.txt     这段音频里逐字准确的内容
    meta.json   {"id": ..., "name": ..., "icon": "female|male"}

三种用法：

1) 已经挑好了某一段音频
   python3 make_voice.py --name "我的声音" --wav my_clip.wav --text "这段录音里说的话"

2) 有一个文件夹的候选片段，让脚本帮你挑
   python3 make_voice.py --name "我的声音" --dir clips/ --list       # 只列候选
   python3 make_voice.py --name "我的声音" --dir clips/ --pick 2     # 选第 2 个
   python3 make_voice.py --name "我的声音" --dir clips/ --text "第一段说的话"

3) 候选片段还带一份文字清单（CSV，表头 file,text[,tag]）
   python3 make_voice.py --name "我的声音" --dir clips/ --manifest clips.csv

挑参考的经验（脚本就是按这个打分的）：
  * 3–8 秒最合适，太短音色不稳，太长没必要
  * 要干声：没有背景音乐、没有混响、没有音效
  * 文字里不要有拖音/省略号之类不是字的东西（~ ～ … — ♪）
"""

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VOICES_DIR = os.path.join(HERE, "voices")

AUDIO_EXT = (".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".opus")
# 这些不是「字」。当成参考文本喂给模型，会让它对齐错，合成结果开头冒出随机杂音
NOISE_CHARS = "~～…—♪"
IDEAL_MIN, IDEAL_MAX = 3.0, 8.0
GOOD_MIN, GOOD_MAX = 4.0, 6.5


def probe_duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30).stdout.strip()
        return float(out)
    except (ValueError, subprocess.SubprocessError, OSError):
        return None


def score(dur, text):
    """返回 (分数, 理由)，不适合则返回 None。"""
    if text is not None:
        if not text.strip():
            return None
        if any(c in text for c in NOISE_CHARS):
            return None                    # 含非语音符号，模型会对不齐
    if dur < IDEAL_MIN or dur > IDEAL_MAX:
        return None                        # 超出可接受时长

    s, why = 0, []
    if GOOD_MIN <= dur <= GOOD_MAX:
        s += 30
        why.append("时长合适")
    else:
        s += 10
    if text:
        n = len(text.replace(" ", ""))
        if 8 <= n <= 25:
            s += 15
            why.append("%d 字" % n)
        elif n < 4:
            return None                    # 太短，模型对不齐
        else:
            s += 5
    why.append("%.2fs" % dur)
    return s, "、".join(why)


def load_manifest(path):
    """读取候选音频的文字清单。接受 file,text 或 file,text,tag 两种表头。"""
    out = {}
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            key = row.get("file") or row.get("path") or row.get("wav")
            if not key:
                continue
            out[os.path.basename(key)] = row.get("text") or row.get("line") or ""
    return out


def collect(dirpath, manifest=None):
    texts = load_manifest(manifest) if manifest else {}
    found = []
    for root, _dirs, files in os.walk(dirpath):
        for fn in sorted(files):
            if not fn.lower().endswith(AUDIO_EXT):
                continue
            p = os.path.join(root, fn)
            dur = probe_duration(p)
            if dur is None:
                continue
            text = texts.get(fn) if texts else None
            if texts and text is None:
                continue                   # 有清单但这份没有对应文字，跳过
            sc = score(dur, text)
            if sc is None:
                continue
            found.append({"path": p, "dur": dur, "text": text or "",
                          "score": sc[0], "why": sc[1]})
    found.sort(key=lambda x: (-x["score"], -x["dur"]))
    return found


def list_voices():
    if not os.path.isdir(VOICES_DIR):
        print("还没有音色")
        return
    ds = sorted(d for d in os.listdir(VOICES_DIR)
                if os.path.isdir(os.path.join(VOICES_DIR, d)))
    if not ds:
        print("还没有音色")
        return
    print("现有音色 %d 个：" % len(ds))
    for d in ds:
        p = os.path.join(VOICES_DIR, d)
        vid, name = d, d
        mp = os.path.join(p, "meta.json")
        if os.path.isfile(mp):
            try:
                m = json.load(open(mp, encoding="utf-8"))
                vid = m.get("id", d)
                name = m.get("name", d)
            except (OSError, ValueError):
                pass
        wav = os.path.join(p, "ref.wav")
        dur = probe_duration(wav) if os.path.isfile(wav) else None
        txt = ""
        tp = os.path.join(p, "ref.txt")
        if os.path.isfile(tp):
            txt = open(tp, encoding="utf-8").read().strip()[:24]
        print("  %-14s %-18s %-7s %s"
              % (vid, name, ("%.1fs" % dur) if dur else "-", txt))


def write_voice(name, vid, icon, wav_path, text):
    dst = os.path.join(VOICES_DIR, name)
    os.makedirs(dst, exist_ok=True)
    shutil.copy(wav_path, os.path.join(dst, "ref.wav"))
    with open(os.path.join(dst, "ref.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    with open(os.path.join(dst, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"id": vid, "name": name, "icon": icon},
                  f, ensure_ascii=False, indent=2)
    return dst


def main():
    ap = argparse.ArgumentParser(
        description="建一个音色（或查看现有音色）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", help="音色名，同时作为 voices/ 下的文件夹名")
    ap.add_argument("--id", default="", help="音色 id（插件传给服务端的值，默认同 --name）")
    ap.add_argument("--icon", default="female", choices=["female", "male"])
    ap.add_argument("--wav", help="直接指定参考音频文件")
    ap.add_argument("--text", default="", help="参考音频里逐字准确的内容")
    ap.add_argument("--dir", help="候选片段所在文件夹，交给脚本挑")
    ap.add_argument("--manifest", help="候选片段的文字清单 CSV（表头 file,text）")
    ap.add_argument("--pick", type=int, default=1, help="选第几个候选（默认 1）")
    ap.add_argument("--list", action="store_true", help="列出候选或现有音色")
    ap.add_argument("--dry-run", action="store_true", help="只看不写")
    args = ap.parse_args()

    if args.list and not args.name:
        list_voices()
        return

    if args.wav:
        if not os.path.isfile(args.wav):
            sys.exit("找不到音频文件: %s" % args.wav)
        if not args.text.strip():
            sys.exit("用 --wav 时必须同时给 --text（参考音频里说了什么）")
        src_text, src_path, src_dur = args.text, args.wav, probe_duration(args.wav)
        print("参考音频: %s" % args.wav)
        print("  时长 %.2f 秒 | 文本: %s" % (src_dur or -1, src_text))
        if src_dur and not (IDEAL_MIN <= src_dur <= IDEAL_MAX):
            print("  ⚠ 建议参考音频在 %.0f–%.0f 秒之间，太长太短都会影响效果"
                  % (IDEAL_MIN, IDEAL_MAX))
        if any(c in src_text for c in NOISE_CHARS):
            print("  ⚠ 参考文本里有 %s 这类非语音符号，建议去掉" % NOISE_CHARS)
    elif args.dir:
        if not os.path.isdir(args.dir):
            sys.exit("找不到文件夹: %s" % args.dir)
        cands = collect(args.dir, args.manifest)
        if not cands:
            sys.exit("在 %s 里没有找到适合当参考的片段（需要 %.0f–%.0f 秒）"
                     % (args.dir, IDEAL_MIN, IDEAL_MAX))
        print("%s：%d 个候选" % (args.dir, len(cands)))
        for i, c in enumerate(cands[:10], 1):
            mark = " ←选中" if i == args.pick else ""
            print("  %2d. %5.2fs  %-40s [%s]%s"
                  % (i, c["dur"], (c["text"] or os.path.basename(c["path"]))[:40],
                     c["why"], mark))
        if args.list or args.dry_run:
            print("\n没有写入任何东西")
            return
        if args.pick < 1 or args.pick > len(cands):
            sys.exit("--pick 超出范围（1-%d）" % len(cands))
        c = cands[args.pick - 1]
        src_path, src_text, src_dur = c["path"], c["text"], c["dur"]
        if not src_text:
            sys.exit("这个候选没有对应文字。请用 --manifest 提供清单，"
                     "或直接用 --wav 指定并配 --text")
    else:
        ap.print_help()
        return

    name = args.name
    if not name:
        sys.exit("需要 --name 指定音色名")
    vid = args.id or name

    dst = write_voice(name, vid, args.icon, src_path, src_text)
    print()
    print("✅ 已建立音色：%s" % name)
    print("   目录: %s" % dst)
    print("   参考: %.2f 秒  %s" % (src_dur or -1, src_text))
    print("   id  : %s" % vid)
    print()
    print("   服务不用重启，插件里刷新音色列表即可。")
    print("   验证: curl -s http://127.0.0.1:8765/voices")


if __name__ == "__main__":
    main()
