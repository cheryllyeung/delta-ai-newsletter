"""LLM 偶爾會在繁體輸出裡夾雜簡體字，這裡做逐字元修正當保險絲。

刻意逐字元轉換而不是整句丟給 OpenCC 的詞彙級繁簡轉換（s2twp 那類），
避免詞彙替換改變字數，也避免「台」「群」「床」這類台灣日常慣用、但
OpenCC 字元對應表裡歸類為簡體的字被誤轉成「臺」「羣」「牀」這種罕用
正體字。
"""
from __future__ import annotations

import opencc

_converter = opencc.OpenCC("s2t")

_KEEP_AS_IS = {"台", "群", "床", "秘"}


def fix_stray_simplified(text: str) -> str:
    return "".join(ch if ch in _KEEP_AS_IS else _converter.convert(ch) for ch in text)


def fix_stray_simplified_in(value):
    """遞迴處理 dict/list/str，用於生成結果的完整 JSON 物件。"""
    if isinstance(value, str):
        return fix_stray_simplified(value)
    if isinstance(value, list):
        return [fix_stray_simplified_in(v) for v in value]
    if isinstance(value, dict):
        return {k: fix_stray_simplified_in(v) for k, v in value.items()}
    return value
