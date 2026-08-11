"""把每次 LLM 呼叫的輸入輸出落地存檔到 runs/{date}/{stage}/。

這是之後校準評分權重、debug 幻覺唯一能用的素材，所以每次呼叫都要留痕，
不是只有失敗才記。
"""
from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path
from typing import Any

RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"

_counters: dict[str, int] = {}

# 保護 _counters 的鎖。打標籤／打分改成多執行緒併發之後，同一個 stage 會有
# 多個執行緒同時要編號，而 `_counters[key] = _counters.get(key, 0) + 1` 是
# 讀出、加一、寫回三個步驟，不是原子操作：兩個執行緒可能都讀到 N、都寫回
# N+1，於是拿到同一個檔名，其中一次呼叫的留痕就被另一次覆蓋掉。
_lock = threading.Lock()


def _next_seq(key: str, stage_dir: Path) -> int:
    """回傳這個 stage 下一個可用的流水號。

    第一次遇到某個 key 時，從磁碟上已有的檔名接續編號，而不是從 0 開始。
    _counters 是行程內的記憶體變數，同一天跑第二次會重新從 001 編號，把前
    一次的留痕整批覆蓋掉——2026-08-10 實際發生過：小樣本那次的
    module_scoring/001~008.json 被同一天稍後的全量執行覆寫。runs/ 是校準
    評分權重、debug 幻覺唯一能用的素材，這種靜默的資料遺失代價太高。
    """
    with _lock:
        if key not in _counters:
            existing = [
                int(p.stem) for p in stage_dir.glob("*.json") if p.stem.isdigit()
            ]
            _counters[key] = max(existing, default=0)
        _counters[key] += 1
        return _counters[key]


def log_call(
    stage: str,
    system: str,
    user: str,
    raw_response: str,
    parsed: Any = None,
    run_date: str | None = None,
) -> Path:
    """記錄一次 LLM 呼叫。stage 例如 "scoring" / "generate" / "self_check" / "intro"。"""
    run_date = run_date or date.today().isoformat()
    stage_dir = RUNS_DIR / run_date / stage
    stage_dir.mkdir(parents=True, exist_ok=True)

    seq = _next_seq(f"{run_date}/{stage}", stage_dir)

    out_path = stage_dir / f"{seq:03d}.json"
    out_path.write_text(
        json.dumps(
            {
                "system": system,
                "user": user,
                "raw_response": raw_response,
                "parsed": parsed,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return out_path
