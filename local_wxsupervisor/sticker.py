"""表情发送：把 MaiBot 的表情消息段转成微信原生表情。

## 为什么不能直接发文本

MaiBot 出站的表情段是：

    {"type": "emoji", "data": "<描述>", "hash": "<sha256>",
     "binary_data_base64": "<图片字节>"}

我们之前把它当纯文本，拼成 `[表情]` / `[emoji]` 发出去，
所以微信里只能看到字面量。要发真表情，必须走 hook 的原生接口：

    POST /SendStickerMsg
    {"wxidorgid": "<群id或wxid>",
     "sticker": {"md5","productid","len","type","width","height"},
     "path": "<state/stickers/outgoing/<md5>.gif>"}
    -> {"ret": 0, "retmsg": "accepted"}

## hook 的严格校验（实测踩出来的）

`wechat_sticker_send.py` 里 `stage_sticker_bytes` 要求：

1. 文件必须落在 `<project_root>/state/stickers/outgoing/` 下
   —— 该路径之外一律 `STICKER_CACHE_PATH_INVALID`
2. 文件必须是 GIF87a / GIF89a（JPEG/PNG/WEBP 要先转 GIF）
3. `fields.md5` 必须等于文件真实 md5
4. `fields.len` 必须精确等于文件字节数
5. 文件 ≤ 8 MB

不满足时 hook 回 400：
    {"ret":1,"error":"Invalid sticker file"}    —— 文件层面
    {"ret":1,"error":"Invalid sticker request"} —— 字段类型不对（len/type 要 int）

## 图片格式

MaiBot 表情库里有 jpeg / gif / png。微信原生表情只收 GIF，
所以非 GIF 必须先转码（多帧保留动画）。转码后的字节 md5 才是
要传给 hook 的 md5，不是原文件的。
"""
from __future__ import annotations

from .perf import MediaLeaseRegistry

import base64
import hashlib
import io
import logging
import pathlib
import re
import uuid
from typing import Any, Mapping

log = logging.getLogger('wxsupervisor.sticker')

MAX_STICKER_BYTES = 8 * 1024 * 1024
MAX_SIDE = 4096
MAX_FRAMES = 64
MAX_PIXELS = 80_000_000

GIF_MAGIC = (b'GIF87a', b'GIF89a')


def _is_gif(raw: bytes) -> bool:
    return raw.startswith(GIF_MAGIC)


def to_native_gif(raw: bytes) -> bytes:
    """把任意图片字节转成微信原生表情可用的 GIF。

    已是 GIF 时原样返回（保留逐帧字节，避免二次编码掉画质）。
    否则用 Pillow 转 GIF，多帧保留动画与时长。
    """
    if not raw:
        raise ValueError('空图片数据')
    if len(raw) > MAX_STICKER_BYTES:
        raise ValueError(f'图片超过 {MAX_STICKER_BYTES // 1024 // 1024}MB')
    if _is_gif(raw):
        return raw

    try:
        from PIL import Image
    except ModuleNotFoundError as exc:
        raise ValueError('需要 Pillow 才能转码非 GIF 表情') from exc

    with Image.open(io.BytesIO(raw)) as image:
        if image.format not in ('JPEG', 'PNG', 'WEBP'):
            raise ValueError(f'不支持的图片格式: {image.format}')
        if not (0 < image.width <= MAX_SIDE and 0 < image.height <= MAX_SIDE):
            raise ValueError('图片尺寸超限')

        frames_count = getattr(image, 'n_frames', 1)
        if frames_count > MAX_FRAMES or frames_count * image.width * image.height > MAX_PIXELS:
            raise ValueError('图片帧数或像素总量超限')

        frames = []
        durations = []
        for index in range(frames_count):
            image.seek(index)
            frames.append(image.convert('RGBA'))
            durations.append(max(20, min(10000, int(image.info.get('duration', 100)))))

        out = io.BytesIO()
        frames[0].save(
            out,
            format='GIF',
            save_all=frames_count > 1,
            append_images=frames[1:],
            duration=durations if frames_count > 1 else durations[0],
            loop=image.info.get('loop', 0),
        )
        result = out.getvalue()

    if not result:
        raise ValueError('转码结果为空')
    if len(result) > MAX_STICKER_BYTES:
        raise ValueError('转码后超过 8MB')
    return result


def stage_gif(project_root: str | pathlib.Path, raw: bytes) -> tuple[pathlib.Path, str, int]:
    """把 GIF 落到 hook 认可的位置，返回 (路径, md5, 字节数)。

    位置必须是 `<root>/state/stickers/outgoing/<md5>.gif`，
    这是 hook 的路径白名单。
    """
    root = pathlib.Path(project_root).resolve()
    scope = (root / 'state' / 'stickers').resolve()
    folder = scope / 'outgoing'

    # 防止目录被替换成符号链接越界
    if folder.exists() and folder.is_symlink():
        raise ValueError('outgoing 目录是符号链接，拒绝写入')
    folder.mkdir(parents=True, exist_ok=True)
    if not folder.resolve().is_relative_to(scope):
        raise ValueError('outgoing 目录越界')

    md5 = hashlib.md5(raw).hexdigest()
    target = folder / f'{md5}.gif'

    if target.is_symlink():
        raise ValueError('目标文件是符号链接')
    if target.exists():
        if target.stat().st_size != len(raw) or target.read_bytes() != raw:
            raise ValueError('同名文件内容不一致')
    else:
        temp = folder / f'{md5}.{uuid.uuid4().hex}.tmp'
        try:
            temp.write_bytes(raw)
            temp.replace(target)
        finally:
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass

    MediaLeaseRegistry.shared().acquire(target)   # V2-8：staging 即租约（含内容寻址复用）
    return target, md5, len(raw)


def decode_segment_data(segment: Mapping[str, Any]) -> bytes:
    """从 MaiBot 的 emoji/image 段里取图片字节。

    优先用 `binary_data_base64`；没有就退回 `data` 里可能内嵌的
    base64 / data URL。
    """
    raw_b64 = segment.get('binary_data_base64')
    if isinstance(raw_b64, str) and raw_b64.strip():
        return base64.b64decode(raw_b64, validate=False)

    data = segment.get('data')
    if isinstance(data, str):
        text = data.strip()
        if text.startswith('base64://'):
            return base64.b64decode(text[len('base64://'):], validate=False)
        if text.startswith('data:') and ';base64,' in text:
            return base64.b64decode(text.split(';base64,', 1)[1], validate=False)
        # 裸 base64（长度像、字符集像）
        if len(text) > 256 and re.fullmatch(r'[A-Za-z0-9+/\r\n=]+', text):
            try:
                return base64.b64decode(text, validate=False)
            except Exception:
                pass
    if isinstance(data, dict):
        inner = data.get('binary_data_base64') or data.get('base64')
        if isinstance(inner, str) and inner.strip():
            return base64.b64decode(inner, validate=False)

    raise ValueError('表情段缺少图片数据（无 binary_data_base64）')


def decode_file_segment(segment: Mapping[str, Any]) -> tuple[bytes, str]:
    """从 file 段取 (字节, 文件名)。

    优先 binary_data_base64；其次 data 里的 base64 / url / file_ref。
    url / file_ref 这里不下载（需要网络与鉴权），交给上层决定。
    """
    name = ''
    data = segment.get('data')
    if isinstance(data, Mapping):
        name = str(data.get('name') or data.get('file_name') or data.get('filename') or '')

    raw_b64 = segment.get('binary_data_base64')
    if not isinstance(raw_b64, str) or not raw_b64.strip():
        if isinstance(data, Mapping):
            raw_b64 = data.get('binary_data_base64') or data.get('base64')
    if isinstance(raw_b64, str) and raw_b64.strip():
        return base64.b64decode(raw_b64, validate=False), name

    # 退回 data 里的 data URL / base64://
    if isinstance(data, str):
        text = data.strip()
        if text.startswith('base64://'):
            return base64.b64decode(text[len('base64://'):], validate=False), name
        if text.startswith('data:') and ';base64,' in text:
            return base64.b64decode(text.split(';base64,', 1)[1], validate=False), name

    raise ValueError('文件段缺少字节数据（只有 url/file_ref 时需先下载）')


def build_sticker_request(target: str, gif_path: pathlib.Path,
                          md5: str, length: int) -> dict[str, Any]:
    """构造 /SendStickerMsg 的请求体。

    len / type / width / height 必须是 int，字符串会让 hook 回
    `Invalid sticker request`。
    """
    return {
        'wxidorgid': target,
        'sticker': {
            'md5': md5,
            'productid': '',
            'len': int(length),
            'type': 2,
            'width': 200,
            'height': 200,
        },
        'path': str(gif_path),
    }


# ---------------------------------------------------------------- 图片

IMAGE_EXT_BY_FORMAT = {
    'JPEG': '.jpeg',
    'PNG': '.png',
    'GIF': '.gif',
    'WEBP': '.webp',
}
# hook 的 /SendImgMsg 认这几个扩展名
IMAGE_MAX_BYTES = 20 * 1024 * 1024


def stage_image(project_root: str | pathlib.Path, raw: bytes) -> pathlib.Path:
    """把图片字节落到磁盘，返回 hook 可读的路径。

    `/SendImgMsg` 只接受本机文件路径，且**扩展名必须和实际格式一致**
    （扩展名不对 hook 回 `Unsupported image extension`）。
    所以这里按真实格式补扩展名，而不是沿用 MinaBot 段的原名字。

    存放位置放在 state/media/outgoing 下，与微信表情目录分开，
    避免 hook 的表情路径白名单把普通图片当成表情处理。
    """
    if not raw:
        raise ValueError('空图片数据')
    if len(raw) > IMAGE_MAX_BYTES:
        raise ValueError(f'图片超过 {IMAGE_MAX_BYTES // 1024 // 1024}MB')

    fmt = detect_image_format(raw)
    if fmt is None:
        raise ValueError('无法识别的图片格式（仅支持 JPEG/PNG/GIF/WEBP）')

    root = pathlib.Path(project_root).resolve()
    folder = root / 'state' / 'media' / 'outgoing'
    if folder.exists() and folder.is_symlink():
        raise ValueError('media/outgoing 是符号链接，拒绝写入')
    folder.mkdir(parents=True, exist_ok=True)
    if not folder.resolve().is_relative_to(root):
        raise ValueError('media/outgoing 越界')

    digest = hashlib.sha256(raw).hexdigest()[:32]
    target = folder / f'{digest}{IMAGE_EXT_BY_FORMAT[fmt]}'

    if target.is_symlink():
        raise ValueError('目标文件是符号链接')
    if target.exists():
        if target.stat().st_size != len(raw):
            raise ValueError('同名文件大小不一致')
    else:
        temp = folder / f'{digest}.{uuid.uuid4().hex}.tmp'
        try:
            temp.write_bytes(raw)
            temp.replace(target)
        finally:
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass

    MediaLeaseRegistry.shared().acquire(target)   # V2-8：staging 即租约（含复用）
    return target


def detect_image_format(raw: bytes) -> str | None:
    """按文件头判断真实格式，不信任扩展名。"""
    if raw.startswith(b'\xff\xd8\xff'):
        return 'JPEG'
    if raw.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'PNG'
    if raw.startswith(GIF_MAGIC):
        return 'GIF'
    if len(raw) >= 12 and raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
        return 'WEBP'
    return None


def _has_voice_bytes(seg: Mapping[str, Any]) -> bool:
    """语音段里到底有没有音频字节。

    上游给语音的形态至少有四种，都要认：

        {"type": "voice", "binary_data_base64": "<b64>"}
        {"type": "voice", "data": "<b64>"}            volc_tts 走这条
        {"type": "voice", "data": "data:audio/...;base64,..."}
        {"type": "voice", "data": {"base64": "<b64>"}}

    判不出来就当没有 —— 降级成占位文本，而不是把 base64 当文本发到微信。
    """
    if seg.get('binary_data_base64'):
        return True
    data = seg.get('data')
    if isinstance(data, str):
        probe = data.strip()
        if not probe:
            return False
        if probe.startswith('base64://'):
            return True
        if probe.startswith('data:') and ';base64,' in probe:
            return True
        # 裸 base64：够长且字符集干净
        return len(probe) > 256 and bool(re.fullmatch(r'[A-Za-z0-9+/\r\n=]+', probe))
    if isinstance(data, Mapping):
        for key in ('binary_data_base64', 'base64', 'voice', 'audio', 'content', 'url', 'path'):
            if data.get(key):
                return True
    return False


def split_segments(raw_message: Any) -> dict[str, Any]:
    """把出站消息段拆成各类媒体。

    返回 {'text': str, 'stickers': [...], 'images': [...],
          'voices': [...], 'files': [...], 'others': [...]}

    每类各走各的 hook 接口；无法处理的降级成占位文本并记进 others。
    """
    text_parts: list[str] = []
    stickers: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    voices: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    others: list[str] = []

    if isinstance(raw_message, str):
        return {'text': raw_message, 'stickers': [], 'images': [],
                'voices': [], 'files': [], 'others': []}

    for seg in raw_message or []:
        if not isinstance(seg, Mapping):
            continue
        stype = str(seg.get('type') or '')
        data = seg.get('data')

        if stype == 'text':
            if isinstance(data, Mapping):
                text_parts.append(str(data.get('text') or ''))
            else:
                text_parts.append(str(data or ''))
            continue

        if stype in ('emoji', 'sticker'):
            stickers.append(dict(seg))
            continue

        if stype == 'image':
            has_bytes = bool(seg.get('binary_data_base64')) or isinstance(data, str)
            if has_bytes:
                images.append(dict(seg))
            else:
                text_parts.append('[图片]')
                others.append('image')
            continue

        if stype == 'voice':
            # volc_tts 走 ctx.send.custom("voice", base64) 时，数据可能落在
            # data 字符串、binary_data_base64 或 data 映射里，三处都要认。
            if _has_voice_bytes(seg):
                voices.append(dict(seg))
            else:
                text_parts.append('[语音]')
                others.append('voice')
            continue

        # OneBot v11 用 record，SnowLuma 用 record，MaiBot 某些路径用 audio。
        # 都是语音，统一按 voice 处理。
        if stype in ('record', 'audio'):
            seg2 = dict(seg)
            seg2['type'] = 'voice'
            if _has_voice_bytes(seg):
                voices.append(seg2)
            else:
                text_parts.append('[语音]')
                others.append(stype)
            continue

        # ctx.send.custom(custom_type, data) 落到网关可能变成 custom 段：
        #   {"type": "custom", "data": {"custom_type": "voice", "data": "<b64>"}}
        # custom_type 是语音时按语音处理，别让它退化成占位符。
        if stype == 'custom' and isinstance(data, Mapping):
            inner_type = str(data.get('custom_type') or data.get('type') or '')
            if inner_type in ('voice', 'record', 'audio'):
                inner = data.get('data', data.get('content'))
                seg2 = {'type': 'voice', 'data': inner}
                if _has_voice_bytes(seg2):
                    voices.append(seg2)
                    continue
            text_parts.append('[%s]' % (inner_type or 'custom'))
            others.append('custom')
            continue

        if stype == 'file':
            if isinstance(data, Mapping) and (
                    seg.get('binary_data_base64') or data.get('url')
                    or data.get('file_ref') or data.get('base64')):
                files.append(dict(seg))
            else:
                text_parts.append('[文件]')
                others.append('file')
            continue

        if stype == 'at':
            name = ''
            if isinstance(data, Mapping):
                name = str(data.get('name') or data.get('qq') or '')
            if name:
                text_parts.append('@' + name)
            continue

        if stype == 'reply':
            continue

        placeholders = {'video': '[视频]', 'face': '[表情]'}
        text_parts.append(placeholders.get(stype, f'[{stype}]'))
        others.append(stype)

    return {'text': ''.join(text_parts), 'stickers': stickers,
            'images': images, 'voices': voices, 'files': files,
            'others': others}
