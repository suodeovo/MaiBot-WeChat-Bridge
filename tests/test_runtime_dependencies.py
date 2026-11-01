"""运行依赖与首次安装检查；不运行安装器、不访问真实微信。"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import AsyncMock, patch

from helpers import REPOSITORY_DIR, fresh_plugin
from local_wxsupervisor import runtime_check


class RuntimeChecks(unittest.TestCase):
    def fixture(self, base):
        root = Path(base) / 'workspace'
        for name in runtime_check.READER_FILES + runtime_check.ACCOUNT_FILES:
            file = root / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text('{}', encoding='utf-8')
        return root

    def test_missing_workspace_is_explicit(self):
        with tempfile.TemporaryDirectory() as base:
            errors = runtime_check.check_managed_reader(str(Path(base) / 'missing'), sys.executable)
            self.assertIn('PROJECT_ROOT_MISSING', errors[0])

    def test_missing_modules_are_reported_before_probe(self):
        with tempfile.TemporaryDirectory() as base, patch('subprocess.run') as run:
            errors = runtime_check.check_managed_reader(base, sys.executable)
            self.assertTrue(any('READER_FILES_MISSING' in error for error in errors))
            run.assert_not_called()

    def test_complete_files_can_be_checked_without_credentials(self):
        with tempfile.TemporaryDirectory() as base:
            root = self.fixture(base)
            self.assertEqual(runtime_check.check_managed_reader(str(root), sys.executable, probe_dependencies=False), [])

    def test_missing_python_package_is_actionable(self):
        with tempfile.TemporaryDirectory() as base:
            root = self.fixture(base)
            result = subprocess.CompletedProcess([], 0, stdout='{"missing":["sqlcipher3"]}')
            with patch('subprocess.run', return_value=result):
                errors = runtime_check.check_managed_reader(str(root), sys.executable)
            self.assertIn('READER_DEPENDENCIES_MISSING', errors[0])
            self.assertIn('sqlcipher3', errors[0])

    def test_setup_backs_up_and_writes_matching_paths(self):
        spec = importlib.util.spec_from_file_location('setup_fixture', REPOSITORY_DIR / 'tools/prepare-runtime.py')
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        with tempfile.TemporaryDirectory() as base:
            root = self.fixture(base)
            project = {'runtime': {'python': sys.executable}, 'ports': {'weflow': 15031},
                       'hook': {'baseUrl': 'http://127.0.0.1:30001'}}
            (root / 'config/wechatagent.json').write_text(json.dumps(project), encoding='utf-8')
            candidate = Path(base) / 'plugin'
            candidate.mkdir()
            for name in ('runtime_check.py', 'toml_writer.py', 'config.toml.example', 'patches/reader-api.cursor-patch.py'):
                target = candidate / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(REPOSITORY_DIR / name, target)
            setup.REPO = candidate
            result = subprocess.CompletedProcess([], 0, stdout='{"missing":[]}')
            with patch('subprocess.run', return_value=result), patch.dict('os.environ', {'LOCALAPPDATA': base}):
                report = setup.setup(root, patch_reader=True, configure=True)
            data = tomllib.loads((candidate / 'config.toml').read_text(encoding='utf-8'))
            self.assertEqual(data['project']['root'], root.resolve().as_posix())
            self.assertEqual(data['reader']['base_url'], 'http://127.0.0.1:15031')
            self.assertEqual((Path(report['backup']) / 'reader-api.py').read_text(), '{}')
            self.assertEqual((root / 'scripts/reader-api.py').read_bytes(), (candidate / 'patches/reader-api.cursor-patch.py').read_bytes())


class StartupChecks(unittest.IsolatedAsyncioTestCase):
    async def test_missing_reader_prevents_partial_start(self):
        with tempfile.TemporaryDirectory() as base:
            obj, _ = await fresh_plugin(base, reader__autostart=True)
            obj._start_server = AsyncMock()
            with self.assertRaisesRegex(RuntimeError, 'READER_FILES_MISSING'):
                await obj.on_load()
            obj._start_server.assert_not_awaited()
            self.assertIsNone(obj.supervisor)
            obj.workers.shutdown()

    async def test_failed_reader_start_prevents_partial_start(self):
        with tempfile.TemporaryDirectory() as base:
            obj, _ = await fresh_plugin(base, reader__autostart=True)
            obj._ensure_reader = AsyncMock(return_value=False)
            obj._start_server = AsyncMock()
            with patch('local_wxsupervisor.plugin.check_managed_reader', return_value=[]):
                with self.assertRaisesRegex(RuntimeError, 'READER_START_FAILED'):
                    await obj.on_load()
            obj._start_server.assert_not_awaited()
            obj.workers.shutdown()

    async def test_reader_supervisor_uses_configured_port(self):
        with tempfile.TemporaryDirectory() as base:
            obj, _ = await fresh_plugin(base, reader__base_url='http://127.0.0.1:15031')
            script = Path(base) / 'scripts/reader-api.py'
            script.parent.mkdir()
            script.write_text('', encoding='utf-8')
            self.assertEqual(obj._reader_spec().port, 15031)
            obj.workers.shutdown()
