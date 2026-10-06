"""Translation files must be valid for the Home Assistant frontend.

The frontend formats strings as ICU messages: ``<word>`` is read as a markup
tag (an unclosed one breaks the whole text) and ``{word}`` as a placeholder.
"""

from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path
import re
from typing import Any

import pytest

ROOT = Path(__file__).parent.parent / "custom_components" / "nest_local"
FILES = [ROOT / "strings.json", *sorted((ROOT / "translations").glob("*.json"))]
PLACEHOLDERS = {"serial", "port", "error"}


def _strings(data: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(data, dict):
        for key, value in data.items():
            yield from _strings(value, f"{path}.{key}" if path else key)
    elif isinstance(data, str):
        yield path, data


def _keys(data: Any, path: str = "") -> set[str]:
    return {key for key, _ in _strings(data, path)}


@pytest.mark.parametrize("file", FILES, ids=lambda p: p.name)
def test_no_markup_and_known_placeholders(file: Path) -> None:
    for key, text in _strings(json.loads(file.read_text(encoding="utf-8"))):
        assert "<" not in text and ">" not in text, f"{file.name}: {key}"
        for name in re.findall(r"\{([^{}]*)\}", text):
            assert name in PLACEHOLDERS, f"{file.name}: {key} uses {{{name}}}"
        assert text.count("{") == text.count("}"), f"{file.name}: {key}"


@pytest.mark.parametrize("file", FILES[1:], ids=lambda p: p.name)
def test_translations_match_strings(file: Path) -> None:
    expected = _keys(json.loads(FILES[0].read_text(encoding="utf-8")))
    assert _keys(json.loads(file.read_text(encoding="utf-8"))) == expected
