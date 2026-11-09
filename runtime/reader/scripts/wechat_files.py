"""Bounded file-message metadata and verified, account-local file lookup."""
import hashlib
import json
import os
import pathlib
import re
import threading
import time
import xml.etree.ElementTree as ET

FILE_TYPE = (6 << 32) | 49
MAX_FILE_BYTES = 50 * 1024 * 1024
ID = re.compile(r'^[a-f0-9]{64}$')

def safe_file_name(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 180:
        return None
    if value in ('.', '..') or value[-1:] in (' ', '.') or re.search(r'[\\/:*?"<>|\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]', value):
        return None
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', value, re.I):
        return None
    return value

def parse_file_message(kind, text):
    if kind not in (49, FILE_TYPE) or not isinstance(text, str) or len(text) > 131072 or re.search(r'<!DOCTYPE|<!ENTITY', text, re.I):
        return None
    try:
        root = ET.fromstring(text)
        app = root if root.tag == 'appmsg' else root.find('./appmsg') if root.tag == 'msg' else None
        if app is None or app.findtext('type') != '6':
            return None
        name = safe_file_name(app.findtext('title') or '')
        size = int(app.findtext('appattach/totallen') or '-1')
        md5 = (app.findtext('appattach/md5') or app.findtext('md5') or '').lower()
        if not name or not 0 <= size <= MAX_FILE_BYTES or md5 and not re.fullmatch(r'[a-f0-9]{32}', md5):
            return None
        return {'name': name, 'size': size, 'md5': md5}
    except (ET.ParseError, ValueError, TypeError):
        return None

def file_policy(project, key, sender):
    w = project.get('wechat', {})
    match = re.fullmatch(r'wechat:(private|group):([1-9]\d{0,15})', key or '')
    if not match or not isinstance(sender, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', sender):
        return False
    if match[2] not in map(str, w.get('whitelist', {}).get('groups' if match[1] == 'group' else 'private', [])) or w.get('workModes', {}).get(key, 'social') != 'assistant':
        return False
    policy = w.get('assistant', {})
    if policy.get('fileTransferChats', {}).get(key) is not True:
        return False
    return (match[1] == 'group' and policy.get('adminOnlyGroups', {}).get(key) is False) or sender in policy.get('administrators', [])

def _ordinary(path):
    stat = path.lstat()
    return not path.is_symlink() and not (getattr(stat, 'st_file_attributes', 0) & 0x400)

def hashes(path):
    md5, sha = hashlib.md5(), hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            md5.update(block)
            sha.update(block)
    return md5.hexdigest(), sha.hexdigest()

class FileCatalog:
    def __init__(self, root, account, active=lambda: True):
        self.root, self.account, self.active = pathlib.Path(root), pathlib.Path(account).resolve(), active
        self.lock = threading.RLock()
        self.index_file = self.root / 'state/files/incoming.json'
        self.records = {}
        if self.index_file.is_file() and self.index_file.stat().st_size <= 2 * 1024 * 1024:
            value = json.loads(self.index_file.read_text(encoding='utf-8-sig'))
            if value.get('version') != 1 or not isinstance(value.get('entries'), dict):
                raise RuntimeError('FILE_INDEX_INVALID')
            self.records = value['entries']

    def remember(self, payload, key):
        info = payload.get('file')
        if not info or not self.active() or not ID.fullmatch(payload.get('rawid', '')):
            return None
        project = json.loads((self.root / 'config/wechatagent.json').read_text(encoding='utf-8-sig'))
        if not file_policy(project, key, payload.get('talkerId')):
            return None
        file_id = payload['rawid']
        with self.lock:
            self.records[file_id] = {**info, 'account': str(self.account), 'conversationKey': key, 'senderWxid': payload['talkerId'], 'createTime': payload.get('timestamp', 0)}
            while len(self.records) > 1000:
                self.records.pop(next(iter(self.records)))
            self.index_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.index_file.with_suffix('.tmp')
            temporary.write_text(json.dumps({'version': 1, 'entries': self.records}), encoding='utf-8')
            temporary.replace(self.index_file)
        return {'id': file_id, 'name': info['name'], 'size': info['size']}

    def resolve(self, file_id, key, sender):
        if not ID.fullmatch(file_id or '') or not self.active():
            raise PermissionError('FILE_ACCOUNT_CHANGED')
        with self.lock:
            record = dict(self.records.get(file_id, {}))
        project = json.loads((self.root / 'config/wechatagent.json').read_text(encoding='utf-8-sig'))
        if record.get('account') != str(self.account) or record.get('conversationKey') != key or record.get('senderWxid') != sender or not file_policy(project, key, sender):
            raise PermissionError('FILE_REFERENCE_NOT_ALLOWED')
        if not safe_file_name(record.get('name')) or not isinstance(record.get('size'), int) or not 0 <= record['size'] <= MAX_FILE_BYTES:
            raise RuntimeError('FILE_INDEX_INVALID')
        # XML never supplies a filesystem path. Scan only WeChat's account file
        # folders, with a hard bound, and reject links/reparse points throughout.
        candidates, visited, deadline = [], 0, time.monotonic() + .5
        roots = [self.account / 'msg/file', self.account / 'FileStorage/File']
        for folder in roots:
            if not folder.is_dir():
                continue
            current = folder
            while current != self.account:
                if not _ordinary(current):
                    raise PermissionError('FILE_CACHE_LINK_NOT_ALLOWED')
                current = current.parent
            pending = [(folder, 0)]
            while pending and visited < 3000 and time.monotonic() < deadline:
                directory, depth = pending.pop()
                for entry in os.scandir(directory):
                    visited += 1
                    if visited > 3000 or time.monotonic() >= deadline:
                        break
                    item = pathlib.Path(entry.path)
                    try:
                        if not _ordinary(item):
                            continue
                        if entry.is_dir(follow_symlinks=False) and depth < 3:
                            pending.append((item, depth + 1))
                        elif entry.is_file(follow_symlinks=False) and entry.name.casefold() == record['name'].casefold():
                            stat = item.stat()
                            if stat.st_size == record['size'] and (record.get('md5') or record.get('createTime', 0) - 120 <= stat.st_mtime <= time.time() + 60):
                                candidates.append(item)
                    except OSError:
                        continue
        if not candidates:
            return {'status': 'not_downloaded', 'id': file_id, 'name': record['name'], 'message': '电脑微信尚无可验证的完整文件，请在匹配微信中下载该文件后重试；不会阻塞后续消息。'}
        if not record.get('md5') and len(candidates) != 1:
            return {'status': 'ambiguous', 'id': file_id, 'message': '有同名同大小的文件，无法确认原消息对应的文件。'}
        for item in candidates[:4]:
            md5, sha = hashes(item)
            if record.get('md5') and record['md5'] != md5:
                continue
            if not self.active() or not file_policy(json.loads((self.root / 'config/wechatagent.json').read_text(encoding='utf-8-sig')), key, sender):
                raise PermissionError('FILE_REFERENCE_NOT_ALLOWED')
            return {'status': 'ready', 'id': file_id, 'conversationKey': key, 'senderWxid': sender, 'name': record['name'], 'size': record['size'], 'path': str(item.resolve()), 'accountRoot': str(self.account), 'md5': md5, 'sha256': sha}
        return {'status': 'integrity_failed', 'id': file_id, 'message': '本机文件与微信消息摘要不一致，未向模型提供文件。'}
