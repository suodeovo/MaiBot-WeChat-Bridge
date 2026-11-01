"""豆包语音合成 + SILK 转码。

## 链路

    MaiBot 回复文本
      → 调用 volc_tts 插件的 public API「synthesize」
        （@API("synthesize"), public=True，返回 base64 音频）
      → PCM/WAV 包成标准 wav
      → pilk.encode(..., tencent=True)  → \\x02#!SILK_V3
      → hook /WeChatAgent/voice-send  → 微信语音消息

## 为什么用 pcm 而不是 mp3

volc_tts 的 `audio_format` 可选 `mp3` / `ogg_opus` / `pcm`。
MaiBot 环境里没有任何 mp3 解码器（pydub / miniaudio / soundfile / av 全无，
ffmpeg 也不在 PATH），但有 pilk（SILK 编码器，只吃 wav/pcm）。
所以让 TTS 直接给 pcm，本地包一层 wav 头就能喂给 pilk —— 零额外依赖。

如果配置仍是 mp3，这里也能处理，但需要外部解码器；会明确报错而不是静默失败。

## pilk 的两个坑

1. `pilk.encode()` 的**返回值不是时长**（实测返回 1，与帧数相关）。
   真实时长要用 `pilk.get_duration(path)`，单位毫秒。
2. `tencent=True` 才会加 `\\x02` 前缀，这是微信语音要求的格式。
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import pathlib
import sys
import tempfile
import wave
from typing import Any

log = logging.getLogger('wxsupervisor.tts')

# pilk 搜索路径由配置驱动（WX-009/复核）：plugin._apply_dependency_paths()
# 在加载时注入；此处保留旧默认值仅作兜底。
PILK_SEARCH_PATHS: tuple[str, ...] = ()

# 豆包语音合成 2.0 插件（volc_tts）暴露的 public API。
TTS_API_FULL_NAME = 'local.volc.tts2.synthesize'
TTS_API_SHORT_NAME = 'synthesize'

_pilk = None
_pilk_error = ''


def load_pilk():
    """加载 pilk（SILK 编解码）。失败返回 None 并记住原因。"""
    global _pilk, _pilk_error
    if _pilk is not None:
        return _pilk
    try:
        import pilk  # type: ignore
        _pilk = pilk
        return _pilk
    except ModuleNotFoundError:
        pass
    for path in PILK_SEARCH_PATHS:
        if not pathlib.Path(path).is_dir():
            continue
        if path not in sys.path:
            sys.path.insert(0, path)
        try:
            import pilk  # type: ignore
            _pilk = pilk
            log.info('pilk 已从 %s 加载', path)
            return _pilk
        except Exception as exc:
            _pilk_error = f'{type(exc).__name__}: {exc}'
    _pilk_error = _pilk_error or 'pilk 不可用（既没装也没在已知路径找到）'
    return None


def pcm_to_wav(pcm: bytes, sample_rate: int = 24000,
               channels: int = 1, width: int = 2) -> bytes:
    """把裸 PCM 包成标准 WAV（pilk 需要 WAV 头）。"""
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def wav_duration_ms(wav_bytes: bytes) -> int:
    """从 WAV 头算时长。"""
    try:
        with wave.open(io.BytesIO(wav_bytes), 'rb') as w:
            frames = w.getnframes()
            rate = w.getframerate() or 1
            return max(1, int(round(frames / float(rate) * 1000)))
    except Exception:
        return 0


def encode_wav_to_silk(wav_bytes: bytes, sample_rate: int = 24000) -> tuple[bytes, int]:
    """WAV -> 腾讯格式 SILK。

    返回 (silk 字节, 时长毫秒)。
    时长必须用 `pilk.get_duration()` 取——`pilk.encode()` 的返回值不是时长。
    """
    pilk = load_pilk()
    if pilk is None:
        raise RuntimeError(f'SILK 编码器不可用：{_pilk_error}')

    tmp_dir = pathlib.Path(tempfile.gettempdir())
    stem = f'wxsup-tts-{id(wav_bytes):x}'
    wav_path = tmp_dir / f'{stem}.wav'
    silk_path = tmp_dir / f'{stem}.silk'
    try:
        wav_path.write_bytes(wav_bytes)
        pilk.encode(str(wav_path), str(silk_path),
                    pcm_rate=int(sample_rate), tencent=True)
        silk = silk_path.read_bytes()
        if not silk.startswith(b'\x02#!SILK_V3'):
            raise RuntimeError('SILK 编码结果格式不对（缺 0x02 前缀）')
        duration = int(pilk.get_duration(str(silk_path)))
        return silk, max(1, duration)
    finally:
        for p in (wav_path, silk_path):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass


def audio_to_silk(audio: bytes, mime_type: str = '',
                  sample_rate: int = 24000) -> tuple[bytes, int]:
    """任意 TTS 输出 -> SILK。

    支持 pcm（裸 PCM）与 wav；mp3 需要外部解码器，这里明确报错。
    """
    if not audio:
        raise ValueError('TTS 返回空音频')

    head = audio[:4]
    if head == b'RIFF':
        return encode_wav_to_silk(audio, sample_rate)

    kind = (mime_type or '').lower()
    if 'pcm' in kind or 'octet-stream' in kind or not kind:
        # 裸 PCM：按 24k / 16bit / 单声道 包装
        wav = pcm_to_wav(audio, sample_rate=sample_rate)
        return encode_wav_to_silk(wav, sample_rate)

    if 'mpeg' in kind or 'mp3' in kind:
        raise RuntimeError(
            'TTS 返回的是 MP3，但本机没有 MP3 解码器'
            '（pydub/miniaudio/soundfile/av 均未安装，ffmpeg 不在 PATH）。'
            '请把 volc_tts 的 tts.audio_format 改成 pcm 或 wav。'
        )

    # 未知格式：赌它是裸 PCM
    log.warning('未知 TTS 音频类型 %r，按裸 PCM 尝试', kind)
    wav = pcm_to_wav(audio, sample_rate=sample_rate)
    return encode_wav_to_silk(wav, sample_rate)


def decode_audio_base64(payload: dict[str, Any]) -> tuple[bytes, str]:
    """从 volc_tts 的 synthesize 返回里取 (音频字节, mime)。"""
    if not isinstance(payload, dict):
        raise RuntimeError('TTS 返回不是字典')
    if not payload.get('success'):
        raise RuntimeError(str(payload.get('error') or 'TTS 合成失败'))
    raw_b64 = payload.get('audio_base64')
    if not isinstance(raw_b64, str) or not raw_b64.strip():
        raise RuntimeError('TTS 返回缺少 audio_base64')
    return base64.b64decode(raw_b64, validate=False), str(payload.get('mime_type') or '')


async def synthesize_via_plugin(api_capability, text: str,
                                speaker: str = '') -> tuple[bytes, str]:
    """调用 volc_tts 插件的 public API「synthesize」合成语音。

    `api_capability` 是 `ctx.api`（PluginContext.api），
    其 `call(api_name, **kwargs)` 是跨插件 API 的正规入口。
    volc_tts 那边用 `@API("synthesize", public=True)` 声明，所以能被调用。

    返回 (音频字节, mime)。
    """
    if api_capability is None:
        raise RuntimeError('api 能力未注入，无法调用 TTS 插件')
    kwargs: dict[str, Any] = {'text': text}
    if speaker:
        kwargs['speaker'] = speaker

    # 先用全名再退短名：SDK 两种都认，但短名要求全局唯一，
    # 装了别的 TTS 插件后可能变歧义，全名永远稳定。
    last_error = ''
    for name in (TTS_API_FULL_NAME, TTS_API_SHORT_NAME):
        try:
            payload = await api_capability.call(name, **kwargs)
        except Exception as exc:
            last_error = f'{name}: {type(exc).__name__}: {exc}'
            log.warning('TTS API 调用失败 %s', last_error)
            continue
        return decode_audio_base64(payload)
    raise RuntimeError('调用 TTS 插件 synthesize 失败 —— ' + last_error)


def clean_for_speech(text: str, limit: int = 300) -> str:
    """清掉不能朗读的标记，并按需截断到 TTS 上限。

    `limit <= 0` 表示不截断（用于长度判定，避免先截后判导致
    「超长」永远判不出来）。
    """
    import re
    s = str(text or '')
    s = re.sub(r'https?://\S+', '', s)          # 链接
    s = re.sub(r'\[[^\]]{1,12}\]', '', s)       # [图片] [表情] 之类占位
    s = re.sub(r'@\S+\s?', '', s)               # @某人
    s = re.sub(r'\s+', ' ', s).strip()
    if limit and limit > 0:
        return s[:limit]
    return s
