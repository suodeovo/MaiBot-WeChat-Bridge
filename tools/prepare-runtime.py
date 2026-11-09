"""检查已有的 reader 运行目录，按需部署补丁和写入插件配置。"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import tomllib
import uuid

REPO = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def setup(root: Path, python: str = '', *, patch_reader=False, configure=False):
    root = root.resolve(strict=True)
    project_file = root / 'config/wechatagent.json'
    if not project_file.is_file():
        raise RuntimeError('READER_ACCOUNT_NOT_CONFIGURED: 请先完成本机微信账号连接')
    project = json.loads(project_file.read_text(encoding='utf-8-sig'))
    expected_version = project.get('hook', {}).get('expectedVersion', '4.1.10.27')
    if expected_version != '4.1.10.27':
        raise RuntimeError('WECHAT_VERSION_MISMATCH: 本版本仅验证微信 4.1.10.27，请先切换到匹配客户端')
    selected = python or str(project.get('runtime', {}).get('python') or '')
    if not selected:
        raise RuntimeError('READER_PYTHON_MISSING: 请通过 --python 指定 reader 解释器')
    executable = Path(selected)
    if not executable.is_absolute():
        executable = root / executable
    executable = executable.resolve()
    checker = load('wx_runtime_checker', REPO / 'runtime_check.py')
    errors = checker.check_managed_reader(str(root), str(executable))
    if errors:
        raise RuntimeError('\n'.join(errors))
    backup = None
    if patch_reader or configure:
        backup_root = Path(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()) / 'MaiBotBridgeBackups'
        backup = backup_root / ('runtime-setup-' + uuid.uuid4().hex[:12])
        backup.mkdir(parents=True, exist_ok=False)
    if patch_reader:
        destination = root / 'scripts/reader-api.py'
        assert destination.resolve().is_relative_to(root)
        shutil.copy2(destination, backup / 'reader-api.py')
        patch = REPO / 'patches/reader-api.cursor-patch.py'
        shutil.copy2(patch, destination)
        assert hashlib.sha256(destination.read_bytes()).digest() == hashlib.sha256(patch.read_bytes()).digest()
    if configure:
        config_file = REPO / 'config.toml'
        if config_file.exists():
            shutil.copy2(config_file, backup / 'config.toml')
        source = config_file if config_file.exists() else REPO / 'config.toml.example'
        data = tomllib.loads(source.read_text(encoding='utf-8-sig'))
        data.setdefault('project', {}).update(root=root.as_posix(), python=executable.as_posix(),
                                             media_scripts_dir=(root / 'scripts').as_posix())
        for field in ('pilk_paths', 'crypto_paths'):
            if 'path\\to' in str(data['project'].get(field, '')):
                data['project'][field] = ''
        port = int(project.get('ports', {}).get('weflow') or 5031)
        data.setdefault('reader', {})['base_url'] = 'http://127.0.0.1:' + str(port)
        data.setdefault('hook', {})['base_url'] = project.get('hook', {}).get('baseUrl') or 'http://127.0.0.1:30001'
        writer = load('wx_runtime_toml_writer', REPO / 'toml_writer.py')
        temporary = config_file.with_name('config.toml.runtime-setup.tmp')
        if temporary.exists():
            raise RuntimeError('CONFIG_UPDATE_PENDING: 请先处理上次遗留的临时文件')
        temporary.write_text(writer.dumps_toml(data), encoding='utf-8')
        temporary.replace(config_file)
    return {'ready': True, 'project_root': str(root), 'python': str(executable),
            'reader_patched': patch_reader, 'plugin_configured': configure,
            'backup': str(backup) if backup else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, required=True, help='已有的 reader 运行目录')
    parser.add_argument('--python', default='', help='可选，默认读取现有运行配置中的解释器路径')
    parser.add_argument('--patch-reader', action='store_true', help='备份后部署本插件的游标 reader 补丁')
    parser.add_argument('--configure', action='store_true', help='备份后写入本插件的路径和连接地址')
    args = parser.parse_args()
    try:
        result = setup(args.project_root, args.python, patch_reader=args.patch_reader, configure=args.configure)
    except (RuntimeError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
