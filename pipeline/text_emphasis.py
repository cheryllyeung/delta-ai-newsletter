"""把文字裡的數字標成重點（2026-09-30 加）。

觀察專欄的版面需要視覺焦點，但不能叫模型自己輸出 HTML：模型吐標籤等於
讓它決定版面，而且一旦它吐出沒閉合的標籤或 script，信件與網頁就壞了。
所以改成程式端處理：先把文字做 HTML 轉義，再用正則把數字與百分比包成
粗體。這樣「哪裡被強調」是規則決定的，不是模型決定的。

只標數字，不標形容詞或名詞：數字是這份刊物真正的重點，也是唯一能被
原文驗證的東西。
"""
from __future__ import annotations

import html
import re

# 數字（含千分位、小數、百分比、倍數、金額單位）與緊接其後的中英文單位。
_NUMBER = re.compile(
    r"(\d[\d,]*(?:\.\d+)?\s*(?:%|％|倍|億|萬|千|百分點|bp|GB|TB|MW|GW|kW|nm|µm|mm|天|週|年|月|日|小時|分鐘|秒|人|例|篇|則|家|個|份|美元|元|歐元)?)"
)


def emphasize_numbers(text: str) -> str:
    """回傳可以直接放進 HTML 的字串（已轉義），數字部分包了 <b>。"""
    escaped = html.escape(text or "")
    return _NUMBER.sub(r"<b>\1</b>", escaped)
