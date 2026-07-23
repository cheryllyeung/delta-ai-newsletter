"""讀取 prompts/*.md 並代入 {{variable}}。

不用 str.format()，因為 prompt 檔裡的 JSON schema 範例本身就大量
使用 { }，用 format 會互相打架。改用正則只認 {{word}} 這種雙大括號格式。
"""
from __future__ import annotations

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

_VAR_PATTERN = re.compile(r"\{\{(\w+)\}\}")


def _extract_tag(raw: str, tag: str) -> re.Match | None:
    return re.search(rf"<{tag}>(.*?)</{tag}>", raw, re.S)


def _read(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def render(text: str, **variables: object) -> str:
    def _sub(match: re.Match) -> str:
        key = match.group(1)
        if key not in variables:
            raise KeyError(f"prompt 缺少變數代入：{{{{{key}}}}}")
        return str(variables[key])

    return _VAR_PATTERN.sub(_sub, text)


def load_prompt_parts(name: str, **variables: object) -> tuple[str, str]:
    """回傳 (system, user) 兩段文字，已代入變數。

    style_guide 會自動注入（讀 prompts/style_guide.md），呼叫端不用自己傳，
    傳了也會被覆蓋以外層檔案為準。
    """
    raw = _read(name)
    variables = {**variables, "style_guide": _read("style_guide").strip()}

    system_match = _extract_tag(raw, "system")
    user_match = _extract_tag(raw, "user")
    if not system_match or not user_match:
        raise ValueError(f"prompts/{name}.md 格式不對，缺少 <system>/<user> 區塊")

    system_text = render(system_match.group(1).strip(), **variables)
    user_text = render(user_match.group(1).strip(), **variables)
    return system_text, user_text
