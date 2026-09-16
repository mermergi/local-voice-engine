# local-voice-engine

**在安卓手机（Termux）上跑一个完全离线的语音合成服务，并接进 [TTS Server](https://github.com/jing332/tts-server-android) 当系统 TTS 用。**

给一段几秒钟的参考音频，它就能用那个音色朗读任意文字。不用训练、不用显卡、不用联网。

> **English**: An offline, CPU-only zero-shot TTS HTTP server that runs in Termux on Android,
> plus a plugin for the TTS Server app. Give it a few seconds of reference audio and it speaks
> in that voice — no training, no GPU, no network.

---

## 它解决什么问题

安卓上的 TTS 要么是联网的在线接口，要么是系统自带的机械音，想换成"某个特定声音"通常得：

* 拿 GPU 训练一个语音模型（几小时到几天）
* 或者依赖在线 API（要联网、要钱、有隐私顾虑）

这个项目走的是**零样本克隆**路线：音色不需要训练进权重里，而是每次合成时把一小段参考音频
喂给底座的 flow-matching 模型。于是：

| | 传统微调路线 | 本项目 |
| --- | --- | --- |
| 训练 | 必须，CPU 上数周 | **完全不需要** |
| 换音色 | 重训一个模型 | **换一段 4 秒音频** |
| 需要显卡 | 是 | **否，纯 CPU** |
| 需要联网 | 训练和推理都要 | **全离线** |
| 首段延迟（46 字） | — | **1.8 秒**（流式） |

实测在 8 核 arm64 手机上 **RTF 0.5–0.7，比实时还快**。

---

## 特点

* **零训练**：3–8 秒参考音频 + 它对应的文字，就能用这个音色说话
* **全离线**：只读本地文件，把 `http_proxy` 指向黑洞也能正常合成
* **纯 CPU**：8 核手机 RTF 0.5–0.7
* **流式输出**：HTTP chunked 逐句推送，不等整段合成完
* **多音色**：一个文件夹一个音色，**加音色不用改代码、不用重启**
* **崩溃自愈**：合成跑在子进程里，底层库 segfault 也不会带走整个服务
* **参考音频格式不限**：mp3 / 24-bit wav / 立体声 / 任意采样率，服务端自动转码
* **无需编译**：`sherpa-onnx` 有原生 `android_arm64` 轮子，`pip install` 即可

---

## 环境要求

* 安卓手机 + [Termux](https://termux.dev/)（本项目在 Android 17 / arm64 上实测）
* Python 3.12+（Termux 自带）
* 约 500 MB 磁盘（模型 185 MB + 你的音色）
* TTS Server 1.25 及以上（可选，不装也能用 HTTP 接口）

---

## 快速开始

### 1. 装依赖

```bash
pkg update
pkg install -y python espeak ffmpeg
pip install sherpa-onnx numpy
```

> `sherpa-onnx` 提供原生 `android_24_arm64_v8a` 轮子，不需要自己编译。

### 2. 下模型

```bash
git clone https://github.com/<你的用户名>/local-voice-engine.git
cd local-voice-engine

python3 download_models.py            # 合成必需，约 185 MB
python3 download_models.py --verify   # 可选：ASR + 声纹模型，用于自检
```

国内网络下 `github.com` / `huggingface.co` 可能不通：

```bash
python3 download_models.py --hf-mirror --gh-proxy https://ghfast.top
```

如果报 `CERTIFICATE_VERIFY_FAILED`（有些网络或代理会做中间人），
确认网络可信后可以加 `--insecure` 跳过证书校验：

```bash
python3 download_models.py --hf-mirror --insecure
```

### 3. 建一个音色

**先准备一段参考音频**（下面「怎么弄到参考音频」有详细建议），然后：

```bash
# 已经挑好了某一段
python3 make_voice.py --name "我的声音" --wav my_clip.wav --text "这段录音里说的话"

# 或者给一个文件夹让脚本帮你挑
python3 make_voice.py --name "我的声音" --dir clips/ --list
python3 make_voice.py --name "我的声音" --dir clips/ --pick 2 --text "第 2 段说的话"
```

音色就是 `voices/<名字>/` 下三个文件：

```
voices/我的声音/
├── ref.wav      参考音频（任意格式）
├── ref.txt      这段音频里逐字准确的内容
└── meta.json    {"id": "...", "name": "...", "icon": "female|male"}
```

> `ref.txt` 必须和音频**逐字对齐**。如果里面混进了 `~ ～ … — ♪` 这类不是字的东西，
> 模型会对齐错，合成结果开头会冒出随机的怪音——这是实测踩过的坑。

### 4. 启动服务

```bash
bash start.sh
```

看到这样就成功了：

```
✅ 已就绪: http://127.0.0.1:8765
{"ok": true, "worker": true, "voices": ["我的声音"], ...}
```

自检：

```bash
curl -s http://127.0.0.1:8765/voices
curl -o t.mp3 'http://127.0.0.1:8765/tts?text=你好'
```

### 5. 接进 TTS Server

1. TTS Server → **插件管理** → 右上角 **添加**
2. 把 `plugin_tts_server.js` 的**全部内容**粘进去 → 保存
3. 插件 **⋮ → 设置变量** → `服务地址` 填 `http://127.0.0.1:8765`
4. 回到 TTS 配置，引擎选 **本地语音引擎**，音色下拉框里就是你的音色

---

## 怎么弄到参考音频

**请只使用你有权使用的音频。** 建议：

* **自己录**（最省事也最没有争议）：安静房间里读一段 5–10 秒的文字，手机自带录音即可。
  然后照着念的内容写 `ref.txt`。
* **公开授权的语音语料**：Common Voice、LibriVox（公版有声书）、
  以及各类 CC / 公有领域数据集。
* 自己已有的、有授权的录音素材。

选参考音频的经验（`make_voice.py` 就是按这个打分的）：

| 要求 | 说明 |
| --- | --- |
| **3–8 秒** | 太短音色不稳，太长没必要。4–6.5 秒最舒服 |
| **干声** | 不要背景音乐、不要混响、不要音效、不要多人对话 |
| **至少 6 个字** | 太短模型对不齐 |
| **文字逐字准确** | 念错了会明显影响效果 |
| **别有 `~ … —`** | 这些不是字，会让模型错位 |

实测：同一段音色，参考音频从 1.3 秒换成 4.9 秒，声纹相似度从 **0.42 涨到 0.69**。
参考太短，克隆质量掉得很明显。

不知道参考音频说了什么？可以用仓库里的 `asr_check.py`（需要 `download_models.py --verify`）
转写一遍再人工核对：

```bash
python3 asr_check.py my_clip.wav
```

---

## HTTP 接口

| 接口 | 说明 |
| --- | --- |
| `GET /voices` | 音色列表 JSON：`[{"id","name","icon"}, ...]` |
| `GET /health` | 健康检查，含 worker 状态、重启次数、当前音色 |
| `GET /tts` | 合成音频 |

`/tts` 参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `text` | 必填 | 要合成的文本 |
| `voice` | 第一个音色 | 音色 id |
| `format` | `mp3` | `mp3` 或 `wav` |
| `speed` | `1.0` | 语速，**只支持 0.5–1.2**（见下） |
| `stream` | `0` | `1` = chunked 流式逐句推送 |

```bash
# 普通合成
curl -o out.mp3 --get --data-urlencode "text=你好，世界" \
     --data-urlencode "voice=我的声音" http://127.0.0.1:8765/tts

# 流式：第一段音频很快就到，后面的边播边到
curl -N -o out.mp3 --get --data-urlencode "text=很长的一段文字……" \
     --data-urlencode "voice=我的声音" "http://127.0.0.1:8765/tts?stream=1"
```

---

## 加音色 / 换音色

```bash
python3 make_voice.py --list                    # 现有音色
python3 make_voice.py --name "新声音" --wav a.wav --text "念的内容"
curl -s http://127.0.0.1:8765/voices            # 不用重启，立刻就能看到
```

删音色就是删 `voices/<名字>/` 整个文件夹。

---

## 实测数据

在 8 核 arm64 安卓手机（Termux，CPU-only）上：

| 项目 | 结果 |
| --- | --- |
| RTF | **0.5–0.7**（比实时快） |
| 46 字 / 4 句，非流式 | 首段音频 **6.84 s** |
| 46 字 / 4 句，流式 | 首段音频 **1.83 s**（快 3.7×） |
| 115 字 / 9 句，非流式 | 首段音频 **11.53 s** |
| 115 字 / 9 句，流式 | 首段音频 **1.51 s**（快 7.6×） |
| 音色相似度（CAM++ 余弦） | 0.69–0.80（同人基线 0.81–0.83，不同人 0.24–0.41） |
| 可懂度 | ASR 回读与输入文本一致 |

流式总耗时和不流式差不多（甚至略长一点，因为句间插了 0.12 秒停顿），
省的纯粹是**第一句话到耳朵的时间**，文本越长优势越大。

---

## 已知限制

### `speed` 只能在 0.5–1.2 之间

底层 ZipVoice 的 `speed` 不是线性变速，超出范围会退化甚至崩溃：

| speed | 结果 |
| --- | --- |
| 0.5 / 1.0 / 1.2 | 正常 |
| 1.5 | 2.5 秒的句子只剩 0.79 秒，**变哑巴** |
| 2.0 | **segfault** |

服务端会强制夹在这个区间。插件把 TTS Server 的 rate(0–100) 映射进去（50 → 1.0）。

### 极短文本偶发出错

2 个字这种输入（"你好"、"测试"）有时会合成成别的词。正常句子（≥6 字）没有这个问题。

### 合成子进程会崩，但已隔离

除了 speed 越界，"极短文本 + speed 偏高" 也会 segfault。
所以合成跑在常驻子进程里，父进程负责拉起；服务本身不会被带走。
`GET /health` 里的 `restarts` 字段可以看到重启过几次。

### 后台保活

Android 会清理后台进程。建议：

* 启动脚本里已经帮你 `termux-wake-lock`
* 在系统设置里给 Termux **关闭电池优化**
* 如果 `curl` 报 connection refused，多半是被清了，重新 `bash start.sh`

---

## 它是怎么工作的

```
文本
 ↓  按句末标点切句
 ↓  espeak-ng 转音素（cmn）
 ↓  音素 → id 序列（BOS/PAD/EOS）
 ↓  ZipVoice（flow matching）: 参考音频 + 参考文本 → 该音色的语音
 ↓  vocos 声码器 → 24 kHz 波形
 ↓  ffmpeg → mp3（流式时逐句推送）
音频
```

`tts_server.py` 是 HTTP 层，`clone_zh.py` 是合成内核，两者可以分开用。

---

## 文件

| 文件 | 说明 |
| --- | --- |
| `tts_server.py` | HTTP 服务（多音色 + 流式 + 子进程隔离 + 崩溃自愈） |
| `plugin_tts_server.js` | TTS Server 插件（音色列表动态获取） |
| `make_voice.py` | 建音色、挑参考片段 |
| `download_models.py` | 下载模型（含给声纹模型补 metadata） |
| `clone_zh.py` | 合成内核（命令行直接可用） |
| `start.sh` / `stopsrv.py` | 后台启动 / 停止 |
| `asr_check.py` | ASR 回读，验证合成说了什么（可选） |
| `speaker_sim.py` | 声纹相似度，验证像不像（可选） |

---

## 第三方组件与许可

本仓库**不包含任何模型文件**，模型由 `download_models.py` 从各自官方地址下载，
各自遵循其原始许可：

| 组件 | 用途 | 许可 |
| --- | --- | --- |
| [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) | 推理框架 | Apache-2.0 |
| ZipVoice（emilia 蒸馏版） | 零样本语音合成 | Apache-2.0 |
| [vocos](https://github.com/charactr-platform/vocos) | 声码器 | MIT |
| [SenseVoice](https://github.com/FunAudioLLM/SenseVoice) | ASR，自检用 | Apache-2.0 |
| CAM++ / 3D-Speaker | 声纹，自检用 | Apache-2.0 |

使用前请自行确认各模型的许可与适用范围。

## 免责声明

本项目只是把模型和推理框架接在一起。**音色的合法性与伦理责任在使用者**：

* 请只使用你拥有权利或已获授权的音频作为参考
* 不要用别人的声音冒充他人、不要用于诈骗或误导
* 合成内容建议明确标注为 AI 生成

---

## License

MIT（仅指本仓库代码，不含第三方模型）
