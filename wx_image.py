"""微信图片 → 标准图片段（W4）。

## 主路径：reader 的图片解析服务（零重复造轮子）

reader-api 内置了完整链路（密钥推导、dat 解码、归一化、缓存），
并暴露 HTTP 接口：

    GET /api/v1/images/<image_id>
    image_id = sha256(json([ownWxid, talker, rawid]))
    -> {"success": true, "b64": "<jpeg/png/webp base64>",
        "mime": "image/jpeg", "width": ..., "height": ...}

rawid 就是推送里的 `rawid` 字段（我们投递时也用它当 message_id），
所以收到图片推送时一步就能取到解码后的字节，直接组装 image 段。

## 降级路径：本地 dat 解码

reader 不可达或该图未注册时，退回本地解析：
密钥推导 + V2 容器解码（复用 WeChatAgent 的 wechat_media，
pycryptodome 从 WeChatAgent runtime 路径注入，同 pilk 模式）。
密钥只在内存，不落盘。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import pathlib
import re
import sys
import urllib.request
from typing import Any, Mapping

log = logging.getLogger('wxsupervisor.wximg')

CRYPTO_SEARCH_PATHS: tuple[str, ...] = ()  # 配置驱动（WX-009/复核）

MEDIA_SCRIPTS_DIR: str = ''   # 配置驱动（WX-009/复核）

V2_MAGIC = b"\x07\x08V2\x08\x07"
V2_CIPHERTEXT_START = 15

_crypto_ok = False
_media_mod = None


def _ensure_local_decoder() -> bool:
    """按需加载本地解码依赖（降级路径才用）。"""
    global _crypto_ok, _media_mod
    if _crypto_ok and _media_mod is not None:
        return True
    if not _crypto_ok:
        try:
            from Crypto.Cipher import AES  # noqa: F401
            from Crypto.Util import Padding  # noqa: F401
            _crypto_ok = True
        except ModuleNotFoundError:
            for path in CRYPTO_SEARCH_PATHS:
                if not pathlib.Path(path).is_dir():
                    continue
                if path not in sys.path:
                    sys.path.insert(0, path)
                try:
                    from Crypto.Cipher import AES  # noqa: F401
                    from Crypto.Util import Padding  # noqa: F401
                    _crypto_ok = True
                    log.info('pycryptodome 已从 %s 加载', path)
                    break
                except Exception as exc:
                    log.debug('Crypto 加载失败 %s: %s', path, exc)
        if not _crypto_ok:
            log.warning('本地图片解码不可用：无 pycryptodome')
            return False
    if _media_mod is None:
        scripts = pathlib.Path(MEDIA_SCRIPTS_DIR) if MEDIA_SCRIPTS_DIR else None
        if scripts and scripts.is_dir() and str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        import wechat_media
        _media_mod = wechat_media
    return True


class ImageResolver:
    """图片解析：reader 主路径 + 本地降级。实例级缓存密钥。"""

    def __init__(self, reader_base_url: str, reader_token: str) -> None:
        self.base = reader_base_url.rstrip('/')
        self.token = reader_token
        self.xor_key: int | None = None
        self.aes_key: bytes | None = None

    # ---------------- reader 主路径

    def via_reader(self, own_wxid: str, talker: str, rawid: str,
                   timeout: float = 20.0) -> tuple[bytes, str] | None:
        image_id = hashlib.sha256(
            json.dumps([own_wxid, talker, rawid]).encode()).hexdigest()
        url = f'{self.base}/api/v1/images/{image_id}'
        req = urllib.request.Request(url, headers={
            'Authorization': 'Bearer ' + self.token})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode('utf-8'))
        except Exception as exc:
            log.debug('reader 图片接口失败 %s: %s', image_id[:12], exc)
            return None
        if not payload.get('success'):
            return None
        b64 = str(payload.get('b64') or '')
        mime = str(payload.get('mime') or '')
        if not b64 or not mime.startswith('image/'):
            return None
        try:
            data = base64.b64decode(b64)
        except Exception:
            return None
        if len(data) < 64:
            return None
        return data, mime

    # ---------------- 本地降级

    def _resolve_key(self, account_dir: str, own_wxid: str) -> bool:
        if self.aes_key is not None:
            return True
        if not _ensure_local_decoder():
            return False
        try:
            res = _media_mod.resolve_v2_media_key(account_dir, own_wxid=own_wxid)
        except Exception as exc:
            log.warning('密钥推导异常: %s', exc)
            return False
        if not res:
            return False
        self.xor_key, self.aes_key = res
        log.info('图片密钥已推导（xor=%d）', self.xor_key)
        return True

    def _local_candidates(self, account_dir: str, talker: str,
                          md5s: list[str]) -> list[pathlib.Path]:
        wanted = {m.lower() for m in md5s
                  if re.fullmatch(r'[a-f0-9]{32}', m or '', re.I)}
        if not wanted:
            return []
        attach = pathlib.Path(account_dir) / 'msg' / 'attach' / hashlib.md5(
            talker.encode()).hexdigest()
        if not attach.is_dir():
            return []
        found: list[tuple[int, int, pathlib.Path]] = []
        for month in sorted(attach.iterdir(), reverse=True):
            folder = month / 'Img'
            if not folder.is_dir():
                continue
            for entry in folder.iterdir():
                m = re.fullmatch(r'([a-f0-9]{32})(_h|_t)?\.dat', entry.name, re.I)
                if not m or m.group(1).lower() not in wanted:
                    continue
                try:
                    info = entry.stat()
                except OSError:
                    continue
                is_thumb = (m.group(2) or '').lower() == '_t'
                found.append((0 if is_thumb else 1, info.st_size, entry))
        found.sort(key=lambda t: (-t[0], -t[1]))
        return [p for _, _, p in found]

    def via_local(self, account_dir: str, own_wxid: str, talker: str,
                  md5s: list[str]) -> tuple[bytes, str] | None:
        if not self._resolve_key(account_dir, own_wxid):
            return None
        for dat in self._local_candidates(account_dir, talker, md5s):
            try:
                out = _media_mod.decode_wechat_v2(
                    str(dat), self.xor_key, self.aes_key)
            except Exception as exc:
                log.debug('dat 解码异常 %s: %s', dat.name, exc)
                continue
            if not out:
                continue
            data, mime = out
            if mime == 'image/x-wxgf' or len(data) < 64:
                continue
            log.info('图片本地解码 %s %d 字节', dat.name, len(data))
            return data, mime
        return None


def image_segment(data: bytes) -> dict[str, Any]:
    """字节 → 标准 image 段（核心自动 VLM 描述）。"""
    return {
        'type': 'image', 'data': '',
        'hash': hashlib.sha256(data).hexdigest(),
        'binary_data_base64': base64.b64encode(data).decode('ascii'),
    }


def enrich_image_payload(payload: Mapping[str, Any], resolver: ImageResolver,
                         own_wxid: str, account_dir: str) -> dict[str, Any] | None:
    """reader 图片推送 → image 段。失败返回 None（调用方降级为占位文本）。

    rawid 必须与 reader 侧 register_image 的 id 一致：
    sha256(json([ownWxid, talker, rawid]))，rawid 即推送的 rawid 字段。
    """
    if not isinstance(payload.get('image'), Mapping):
        return None
    talker = str(payload.get('sessionId') or '')
    rawid = str(payload.get('rawid') or '')
    if not talker or not rawid:
        return None

    out = resolver.via_reader(own_wxid, talker, rawid)
    if out:
        data, _mime = out
        return image_segment(data)

    # 本地降级：md5 从推送或 WeFlow 配置拿不到时只能空跑
    info = payload['image']
    md5s = list(info.get('md5s') or [])
    for key in ('fileMd5', 'md5'):
        v = str(info.get(key) or '').lower()
        if re.fullmatch(r'[a-f0-9]{32}', v):
            md5s.insert(0, v)
    if account_dir and md5s:
        out = resolver.via_local(account_dir, own_wxid, talker, md5s)
        if out:
            data, _mime = out
            return image_segment(data)
    return None


def account_dir_of(plugin_cfg):
    """微信账号目录：显式配置优先，其次 WeFlow-config.json 的 dbPath。"""
    import json as _json
    direct = str(plugin_cfg.get('wechat_account_dir') or '')
    if direct and pathlib.Path(direct).is_dir():
        return direct
    try:
        cfg = _json.loads(pathlib.Path(
            r'C:\\path\\to\\wechatagent\\state\\weflow\\WeFlow-config.json'
        ).read_text(encoding='utf-8-sig'))
        p = str(cfg.get('dbPath') or '')
        if p and pathlib.Path(p).is_dir():
            return p
    except Exception:
        pass
    return ''
