// TTS Server 插件 —— 本地语音引擎（音色列表动态获取，加音色不用改插件）
//
// 用法：TTS Server -> 插件管理 -> 右上角添加 -> 把本文件全部内容粘进去 -> 保存
//       然后 更多选项(⋮) -> 设置变量 -> 服务地址 填 http://127.0.0.1:8765
//
// 音色从服务端 GET /voices 实时拉取。新增音色只要在服务端
//     <引擎目录>/voices/<音色名>/
// 下放 ref.wav + ref.txt（可选 meta.json），**不用改这里，也不用重启服务**。
//
// 说明：getAudio 直接返回一个 http:// 链接，由 TTS Server 自己去取音频，
//       这样不依赖 JS 运行时里 HTTP 客户端的具体版本差异。

let server = (ttsrv.userVars['server'] || 'http://127.0.0.1:8765') + ''
server = server.replace(/\/+$/, '')   // 去掉结尾斜杠

// 流式开关：1 = 边合成边发（首段音频来得早得多），0 = 攒完整段再返回。
// 如果播放异常/没声音，把变量里的 "流式输出" 改成 0 试试。
let streamVar = (ttsrv.userVars['stream'] === undefined) ? '1' : (ttsrv.userVars['stream'] + '')
let useStream = (streamVar === '1' || streamVar === 'true' || streamVar === 'on')

// 拉不到 /voices 时显示这个，提示是服务没连上，而不是音色列表坏了
let OFFLINE_VOICES = {
    'offline': { name: '⚠ 服务未连接', icon: 'female' },
}

function fetchVoices() {
    try {
        let resp = ttsrv.httpGetString(server + '/voices', {})
        if (!resp) {
            return OFFLINE_VOICES
        }
        let list = JSON.parse(resp)
        if (!list || list.length === 0) {
            return OFFLINE_VOICES
        }
        let out = {}
        for (let i = 0; i < list.length; i++) {
            let v = list[i]
            out[v.id] = {
                name: v.name || v.id,
                icon: v.icon || 'female',
            }
        }
        return out
    } catch (e) {
        return OFFLINE_VOICES
    }
}

let PluginJS = {
    "name": "本地语音引擎",
    "id": "local.voice.engine",
    "author": "local-voice-engine",
    "description": "调用本机（Termux）里的离线语音合成服务。音色列表自动获取，加音色不用改插件。",
    "version": 1,

    "vars": {
        server: {
            label: "服务地址",
            hint: "默认 http://127.0.0.1:8765（与 Termux 在同一台手机上时）",
        },
        stream: {
            label: "流式输出",
            hint: "1 = 边合成边播（推荐，首段更快）；0 = 攒完整段再返回。播放异常时改 0",
        },
    },

    // rate/volume/pitch 都是 0-100，50 为正常。
    // 服务端对 speed 有安全区间限制（见 README），这里把 0-100 映射进去。
    "getAudio": function (text, locale, voice, rate, volume, pitch) {
        let r = (rate == null) ? 50 : rate
        if (r < 0) r = 0
        if (r > 100) r = 100

        let speed
        if (r <= 50) {
            speed = 0.5 + (r / 50) * 0.5          // 0 -> 0.5, 50 -> 1.0
        } else {
            speed = 1.0 + ((r - 50) / 50) * 0.2   // 50 -> 1.0, 100 -> 1.2
        }
        speed = Math.round(speed * 100) / 100

        let url = server + '/tts?format=mp3&speed=' + speed +
                  '&text=' + encodeURIComponent(text)
        if (voice) {
            url += '&voice=' + encodeURIComponent(voice)
        }
        if (useStream) {
            url += '&stream=1'
        }
        return url
    },
}

let EditorJS = {
    // 服务返回 24kHz 单声道
    "getAudioSampleRate": function (locale, voice) {
        return 24000
    },

    // 返回的是 mp3，需要解码
    "isNeedDecode": function (locale, voice) {
        return true
    },

    "getLocales": function () {
        return ['zh-CN']
    },

    // 每次打开配置界面都会调用 —— 这里实时去问服务端有哪些音色
    "getVoices": function (locale) {
        return fetchVoices()
    },
}
