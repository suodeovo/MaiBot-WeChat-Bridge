"""最小 TOML 写出器（宿主环境无 tomli_w；tomllib 只读）。

覆盖插件配置的值域：str / bool / int / float / 表（dict）/ 数组（标量）。
字符串按 TOML basic string 转义；键不含特殊字符时裸写。
"""
from __future__ import annotations

from typing import Any, Mapping

_ESCAPES = {
    '\\': '\\\\', '"': '\\"', '\b': '\\b', '\t': '\\t', '\n': '\\n',
    '\f': '\\f', '\r': '\\r',
}


def _escape_string(value: str) -> str:
    out = ['"']
    for ch in value:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20:
            out.append('\\u%04X' % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return ''.join(out)


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return _escape_string(value)
    raise TypeError(f'unsupported TOML value type: {type(value).__name__}')


def _format_array(values: list[Any]) -> str:
    return '[' + ', '.join(_format_value(v) for v in values) + ']'


def _bare_key(key: str) -> bool:
    return all(ch.isalnum() or ch in '-_' for ch in key) and key != ''


def _key(key: str) -> str:
    return key if _bare_key(key) else _escape_string(key)


def dumps_toml(data: Mapping[str, Any]) -> str:
    """dict -> TOML 文本。表输出为 [section] 块，嵌套表递归。"""
    lines: list[str] = []

    def emit(table: Mapping[str, Any], prefix: str) -> None:
        scalars: list[tuple[str, Any]] = []
        tables: list[tuple[str, Any]] = []
        arrays: list[tuple[str, Any]] = []
        for key, value in table.items():
            if isinstance(value, Mapping):
                tables.append((key, value))
            elif isinstance(value, (list, tuple)):
                arrays.append((key, list(value)))
            else:
                scalars.append((key, value))
        for key, value in scalars:
            lines.append(f'{_key(key)} = {_format_value(value)}')
        for key, value in arrays:
            lines.append(f'{_key(key)} = {_format_array(value)}')
        for key, value in tables:
            full = f'{prefix}.{_key(key)}' if prefix else _key(key)
            if lines:
                lines.append('')
            lines.append(f'[{full}]')
            emit(value, full)

    emit(data, '')
    return '\n'.join(lines) + '\n'
