"""托管 reader 启动前检查：只检查文件和模块，不读取账号凭据内容。"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

READER_FILES = (
    'scripts/reader-api.py', 'scripts/wechat_files.py', 'scripts/wechat_media.py',
    'scripts/wechat_forwarded.py', 'scripts/wechat_quote.py',
    'scripts/wechat_sticker_send.py', 'scripts/wechat_sticker.py',
    'scripts/wechat_sticker_labels.py', 'scripts/wechat_sticker_collection.py',
    'scripts/wechat_sticker_manager.py', 'scripts/wechat_meme_library.py',
    'scripts/wechat_group_bindings.py', 'scripts/wechat_image_normalize.py',
    'scripts/resolve-wechat-image.py',
    'components/weflow-cli/scripts/nt_decrypt.py',
    'components/weflow-cli/scripts/nt_common.py',
)
ACCOUNT_FILES = (
    'config/wechatagent.json', 'state/wechat-io-config.json',
    'state/weflow/WeFlow-config.json',
)
DEPENDENCIES = ('sqlcipher3', 'cryptography', 'zstandard', 'Crypto', 'PIL', 'imageio_ffmpeg')


def check_managed_reader(root: str, python: str, *, probe_dependencies: bool = True) -> list[str]:
    """返回可操作的错误列表；仅在需要本插件托管 reader 时调用。"""
    base = Path(root)
    errors = []
    if not base.is_dir():
        return ['PROJECT_ROOT_MISSING: project.root 必须指向已准备的微信桥接运行目录']
    missing = [name for name in READER_FILES if not (base / name).is_file()]
    if missing:
        errors.append('READER_FILES_MISSING: ' + ', '.join(missing)
                      + '；请按 runtime/README.md 准备 reader 运行环境')
    absent = [name for name in ACCOUNT_FILES if not (base / name).is_file()]
    if absent:
        errors.append('READER_ACCOUNT_NOT_CONFIGURED: ' + ', '.join(absent)
                      + '；请先完成本机微信账号连接与数据库验证')
    if not Path(python).is_file():
        errors.append('READER_PYTHON_MISSING: project.python 必须指向 reader 的 python.exe')
    if errors or not probe_dependencies:
        return errors
    # 使用目标解释器进行只读导入检查，不运行 Reader()，不连接数据库。
    code = (
        'import importlib,json\n'
        'missing=[]\n'
        f'for name in {DEPENDENCIES!r}:\n'
        ' try: importlib.import_module(name)\n'
        ' except (ImportError,OSError): missing.append(name)\n'
        'print(json.dumps({"missing":missing}))\n'
    )
    try:
        result = subprocess.run(
            [python, '-B', '-c', code], capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=25,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            env={**os.environ, 'PYTHONUTF8': '1'},
        )
        data = json.loads(result.stdout.strip().splitlines()[-1])
        missing_packages = data['missing']
        if result.returncode or not isinstance(missing_packages, list):
            raise ValueError('Invalid probe result')
    except (OSError, ValueError, IndexError, KeyError, subprocess.TimeoutExpired):
        return ['READER_PYTHON_UNUSABLE: 目标解释器导入检查失败；请修复 reader Python 环境']
    if missing_packages:
        errors.append('READER_DEPENDENCIES_MISSING: ' + ', '.join(str(x) for x in missing_packages)
                      + '；请按 runtime/reader-requirements.txt 安装到该解释器')
    return errors
