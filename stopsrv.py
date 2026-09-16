#!/data/data/com.termux/files/usr/bin/python3
# -*- coding: utf-8 -*-
"""
停止 tts_server 的旧实例。

为什么单独写一个：`pkill -f tts_server.py` 会把「命令行里恰好提到 tts_server.py」
的进程也杀掉——包括正在调用它的那个 shell（踩过两次坑）。
这里严格限定：
  1. 只匹配解释器名以 python 开头的进程
  2. 排除自己的整条祖先链
"""

import glob
import os
import signal
import sys
import time

MARKER = "tts_server.py"
# 只停本目录的实例：否则装了/跑了两份引擎时，启动一个会把另一个也杀掉
TARGET_DIR = os.path.dirname(os.path.abspath(__file__))


def ancestors(pid=None):
    """从当前进程往上收集所有祖先 PID，绝不误杀。"""
    out = set()
    pid = pid or os.getpid()
    for _ in range(24):
        out.add(pid)
        try:
            stat = open("/proc/%d/stat" % pid).read()
            pid = int(stat.rsplit(")", 1)[1].split()[1])
        except Exception:  # noqa: BLE001
            break
        if pid <= 1:
            break
    return out


def find_targets():
    skip = ancestors()
    found = []
    for d in glob.glob("/proc/[0-9]*"):
        try:
            pid = int(d.split("/")[-1])
        except ValueError:
            continue
        if pid in skip:
            continue
        try:
            raw = open(d + "/cmdline", "rb").read()
        except Exception:  # noqa: BLE001
            continue
        parts = [x.decode("utf-8", "replace") for x in raw.split(b"\0") if x]
        if not parts:
            continue
        if not os.path.basename(parts[0]).startswith("python"):
            continue
        if not any(MARKER in a for a in parts):
            continue
        # 认目录：命令行里带本目录，或者进程的工作目录就是本目录
        if not any(TARGET_DIR in a for a in parts):
            try:
                if os.path.realpath("/proc/%d/cwd" % pid) != os.path.realpath(TARGET_DIR):
                    continue
            except OSError:
                continue
        found.append(pid)
    return found


def kill_all(verbose=True):
    pids = find_targets()
    for p in pids:
        try:
            os.kill(p, signal.SIGTERM)
        except Exception:  # noqa: BLE001
            pass
    if pids:
        time.sleep(1.5)
        for p in pids:
            try:
                os.kill(p, 0)
                os.kill(p, signal.SIGKILL)
            except Exception:  # noqa: BLE001
                pass
    if verbose:
        print("  已停止旧实例: %s" % (pids or "无"))
    return pids


if __name__ == "__main__":
    kill_all()
