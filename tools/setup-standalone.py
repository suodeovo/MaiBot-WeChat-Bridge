"""Prepare a dedicated reader and connect the local WeChat account on Windows."""
from __future__ import annotations

import argparse
import filecmp
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import struct
import subprocess
import sys
import tempfile


REPO = Path(__file__).resolve().parents[1]
VERSION = '4.1.10.27'
CLIENT_DIR = 'wechat-' + VERSION
CLIENT = Path('state/hook') / CLIENT_DIR
KEY = Path('components/weflow-cli/resources/key/win32/x64/wx_key.dll')
KEY_SHA = '9a2f2c6a09a3219afa4748a5e7e513d99c607173c3112c5afcc888996b98248c'
HOOK_URL = 'http://127.0.0.1:30001'
HOOK_SHA = '445282efad3213cf8a65c9e28578db43e6bf1201a28403f08b7b4ec1f41b46ec'
PROJECT = Path('config/wechatagent.json')
MARKER = '.maibot-wechat-runtime'
PYTHON = Path('.venv/Scripts/python.exe')
HOOK_TOKEN = Path('state/hook/native-token.txt')
READER_ACCOUNT = Path('state/weflow/WeFlow-config.json')
READER_IO = Path('state/wechat-io-config.json')


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def write_text(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path, data):
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def prepare(root, assets, wheels=None):
    assets = assets.resolve(strict=True)
    client_assets = assets / CLIENT_DIR
    key = assets / 'native/wx_key.dll'
    if not key.is_file() or hashlib.sha256(key.read_bytes()).hexdigest() != KEY_SHA:
        raise RuntimeError('缺少匹配的账号连接组件，请使用本版本完整包里的 runtime 目录。')
    if not all((client_assets / name).is_file() for name in ('Weixin.exe', 'version.dll', f'{VERSION}/Weixin.dll')):
        raise RuntimeError('缺少匹配微信客户端或 hook，请完整解压发布包。')
    if hashlib.sha256((client_assets / 'version.dll').read_bytes()).hexdigest() != HOOK_SHA:
        raise RuntimeError('hook 校验失败，请重新下载本版本完整包。')
    if root.is_relative_to(REPO) or assets.is_relative_to(root) or root.is_relative_to(assets):
        raise RuntimeError('请将运行目录设在插件和解压目录之外。')
    root.mkdir(parents=True, exist_ok=True)
    marker = root / MARKER
    if not marker.is_file():
        if any(root.iterdir()):
            raise RuntimeError('独立安装需要空目录，已有运行环境请按升级说明处理。')
        marker.touch(exist_ok=False)

    for source in client_assets.rglob('*'):
        if (not source.is_file() or source.suffix.lower() not in {'.exe', '.dll', '.bin', '.ini'}
                or source.name.lower() in {'uninstall.exe', 'wetypeinstaller.exe'}):
            continue
        target = root / CLIENT / source.relative_to(client_assets)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or not filecmp.cmp(source, target, shallow=False):
            shutil.copyfile(source, target)
    shutil.copytree(REPO / 'runtime/reader', root, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copyfile(REPO / 'patches/reader-api.cursor-patch.py', root / 'scripts/reader-api.py')
    (root / KEY).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(key, root / KEY)

    if not (root / PYTHON).is_file():
        subprocess.run([sys.executable, '-m', 'venv', str(root / '.venv')], check=True)
    pip = [str(root / PYTHON), '-m', 'pip', '--disable-pip-version-check', 'install',
           '--no-cache-dir', '--only-binary=:all:', '-r', str(REPO / 'runtime/reader-requirements.txt')]
    if wheels is not None:
        pip.extend(['--no-index', '--find-links', str(wheels.resolve(strict=True))])
    subprocess.run(pip, check=True)
    hook_token = root / HOOK_TOKEN
    if not hook_token.is_file():
        write_text(hook_token, secrets.token_hex(32))
    write_text(root / CLIENT / 'wechatagent-hook.token', hook_token.read_text(encoding='utf-8').strip())


def require_runtime(root):
    if not all((root / path).is_file() for path in (MARKER, PYTHON, CLIENT / 'Weixin.exe',
                                                   CLIENT / 'wechatagent-hook.token', HOOK_TOKEN, KEY)):
        raise RuntimeError('运行目录尚未准备完成，请先执行 prepare。')


def start(root, launcher):
    token = (root / HOOK_TOKEN).read_text(encoding='utf-8').strip()
    result = launcher.open_hook_wechat(str(root), (CLIENT / 'Weixin.exe').as_posix(), VERSION, HOOK_URL, token)
    if not result['ok']:
        raise RuntimeError(result['reason'])
    print(result['reason'])


def connect(root, setup, read_account, db_path='', account_id=''):
    accounts = [account for account in read_account.discover_accounts(db_path) if account['ready']]
    if account_id:
        accounts = [account for account in accounts if account['myWxid'] == account_id]
    if not accounts:
        raise RuntimeError('没有找到已初始化的微信数据库。请先在匹配的微信客户端登录，或用 --db-path 指定账号数据目录。')
    if len(accounts) != 1:
        for account in accounts:
            print(account['myWxid'], account['dbPath'])
        raise RuntimeError('检测到多个微信账号，请用 --db-path 指定当前登录账号的目录。')
    account = accounts[0]
    existing = read_json(root / READER_ACCOUNT) if (root / READER_ACCOUNT).is_file() else {}
    if existing and existing.get('dbPath') != account['dbPath']:
        raise RuntimeError('此运行目录已绑定其他账号，请为新账号创建独立运行目录。')
    result = read_account.connect({
        **account, 'wechatExecutable': str(root / CLIENT / 'Weixin.exe'),
        'autoKey': True, 'decryptKey': existing.get('decryptKey', ''),
    })
    if not result['protectedKey'].startswith('userdpapi:'):
        raise RuntimeError('本机凭据保护失败。')
    state = read_json(root / READER_IO) if (root / READER_IO).is_file() else {}
    state.setdefault('reader_token', secrets.token_hex(32))
    # Reader normalizes account folder suffixes itself when self_wxid is absent.
    write_json(root / READER_IO, state)
    write_json(root / READER_ACCOUNT, {**existing, 'dbPath': result['dbPath'],
                                      'myWxid': result['myWxid'], 'decryptKey': result['protectedKey']})
    if not (root / PROJECT).is_file():
        write_json(root / PROJECT, {
            'runtime': {'python': str(root / PYTHON)},
            'ports': {'weflow': 5031}, 'hook': {'baseUrl': HOOK_URL, 'expectedVersion': VERSION},
            'wechat': {'whitelist': {'groups': [], 'private': []}},
        })
    setup.setup(root, configure=True)
    print('账号已连接，插件路径已写入配置。请用 chats 选择接收的会话。')


def chats(root, reader, keyword, usernames):
    if not usernames:
        for chat in reader.contacts(keyword=keyword):
            print(chat['username'], chat['displayName'])
        return
    contacts = {row['username']: row for row in reader.contacts(limit=max(1, len(reader.names)))}
    allowed = {'groups': [], 'private': []}
    for username in dict.fromkeys(usernames):
        chat = contacts.get(username)
        if not chat or not chat.get('conversationId') or chat['ambiguous']:
            raise RuntimeError('无法唯一确认会话，请核对 contacts 中的微信标识。')
        kind = 'groups' if username.endswith('@chatroom') else 'private'
        allowed[kind].append(chat['conversationId'])
    project = read_json(root / PROJECT)
    project['wechat']['whitelist'] = allowed
    write_json(root / PROJECT, project)
    print(f'已保存接收名单，共 {len(set(usernames))} 个会话。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', required=True, type=Path, help='独立运行目录')
    commands = parser.add_subparsers(dest='command', required=True)
    install = commands.add_parser('prepare', help='准备文件和 reader Python 环境')
    install.add_argument('--assets', type=Path, required=True, help='完整包解压后的 runtime 目录')
    install.add_argument('--wheels', type=Path, help='可选，离线 wheel 目录')
    commands.add_parser('start-client', help='启动配套微信客户端')
    bind = commands.add_parser('connect', help='连接当前登录账号')
    bind.add_argument('--db-path', default='', help='可选，微信账号数据目录')
    bind.add_argument('--account', default='', help='可选，账号目录名')
    select = commands.add_parser('chats', help='查看或设置 reader 接收名单')
    select.add_argument('--search', default='', help='按微信标识或名称搜索')
    select.add_argument('--allow', action='append', default=[], help='允许的微信会话标识，可重复指定')
    args = parser.parse_args()
    root = args.project_root.resolve()
    try:
        if os.name != 'nt' or struct.calcsize('P') != 8:
            raise RuntimeError('此工具需要 Windows x64 和 64 位 Python。')
        if args.command == 'prepare':
            if sys.version_info[:2] != (3, 12):
                raise RuntimeError('请用 64 位 Python 3.12 执行 prepare。')
            prepare(root, args.assets, args.wheels)
            print('运行环境已准备好。请执行 start-client 并登录微信。')
            return 0
        require_runtime(root)
        spec = importlib.util.spec_from_file_location('runtime_prepare', REPO / 'tools/prepare-runtime.py')
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        if args.command == 'start-client':
            start(root, setup.load('wx_launcher', REPO / 'launcher.py'))
            return 0
        if Path(sys.executable).resolve() != (root / PYTHON).resolve():
            raise RuntimeError('请使用运行目录下的 .venv\\Scripts\\python.exe 执行该命令。')
        if args.command == 'connect':
            account = setup.load('wx_reader_setup', root / 'scripts/reader_setup.py')
            try:
                connect(root, setup, account, args.db_path, args.account)
            except account.SetupError as error:
                raise RuntimeError(f'{error}。请确认匹配客户端正在运行，并在提示后重新登录该账号。') from None
        else:
            if not (root / PROJECT).is_file():
                raise RuntimeError('请先执行 connect。')
            reader = setup.load('wx_reader', root / 'scripts/reader-api.py')
            chats(root, reader.Reader(root), args.search, args.allow)
    except (RuntimeError, OSError, ValueError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
