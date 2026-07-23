"""把每次 LLM 呼叫的輸入輸出落地存檔到 runs/{date}/{stage}/。

這是之後校準評分權重、debug 幻覺唯一能用的素材，所以每次呼叫都要留痕，
不是只有失敗才記。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"

_counters: dict[str, int] = {}


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

    key = f"{run_date}/{stage}"
    _counters[key] = _counters.get(key, 0) + 1
    seq = _counters[key]

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
