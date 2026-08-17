"""分階段測試 Clawith Qwen 端點（依 Jeff 建議 1：先用同一篇真實文章把標籤／
打分／寫作／自檢四個階段分開測，確認沒問題再考慮整批切換供應商）。

抓一篇真實文章，依序走過四個階段，每個階段都呼叫正式 pipeline 的同一支
函式（tag_article / score_topic / generate_topic_article / self_check），
只是把 LLM_API_KEY / LLM_BASE_URL / NEWSLETTER_MODEL 暫時換成 Clawith 的
值，測完再還原，不影響 .env 裡正式在跑的 Groq 設定。

驗證順序依 Jeff 建議 5：先看技術面（有沒有成功、JSON 格式合不合規、
花多久），內容面（讀起來好不好、要不要人工修）留給人工複核，這支腳本
不判斷內容品質。

用法：
    python -m scripts.test_clawith_staged
    python -m scripts.test_clawith_staged --compare-groq   # 額外跑一次 Groq 對照（會耗用 Groq 額度，Jeff 建議 4，optional）
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml
from dotenv import load_dotenv

load_dotenv()

from generation.topic_generate import generate_topic_article
from ingestion.case_source import fetch_case_study_items
from pipeline.article_tagging import tag_article
from pipeline.module_scoring import score_topic
from review.topic_selfcheck import self_check

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "topics.yaml"
RESULTS_PATH = Path(__file__).resolve().parent.parent / "docs" / "0730" / "clawith_test_results.json"

# 用一個穩定、內容量適中的來源抓測試文章，不依賴本機資料庫既有資料，
# 這樣同一支腳本在任何一台機器上都能自己生出一篇真實文章來測。
_TEST_SOURCE = {
    "source_id": "aws_ml_blog",
    "source_name": "AWS Machine Learning Blog",
    "url": "https://aws.amazon.com/blogs/machine-learning/feed/",
    "weight": 1.0,
}


def fetch_one_real_article():
    items = fetch_case_study_items(
        source_id=_TEST_SOURCE["source_id"],
        source_name=_TEST_SOURCE["source_name"],
        url=_TEST_SOURCE["url"],
        weight=_TEST_SOURCE["weight"],
        days_back=30,
        max_items=1,
    )
    if not items:
        raise SystemExit(f"抓不到測試用文章（{_TEST_SOURCE['url']}），換個來源試試。")
    return items[0]


def run_stage(name: str, fn):
    t0 = time.time()
    try:
        result = fn()
        return {
            "stage": name,
            "ok": True,
            "seconds": round(time.time() - t0, 2),
            "result": result,
            "error": None,
            "error_type": None,
        }
    except Exception as exc:  # noqa: BLE001 -- 這支是診斷腳本，任何錯誤都要接住並記錄下來，不能中斷整輪測試
        return {
            "stage": name,
            "ok": False,
            "seconds": round(time.time() - t0, 2),
            "result": None,
            "error": f"{type(exc).__name__}: {exc}",
            "error_type": type(exc).__name__,
        }


def run_staged_test(label: str) -> list[dict]:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    item = fetch_one_real_article()
    print(f"\n=== {label}：測試文章「{item.title[:50]}」===")
    results = []

    r1 = run_stage("標籤", lambda: tag_article(item))
    results.append(r1)
    _print_stage(r1)
    if not r1["ok"]:
        return results
    tags = r1["result"]

    article_row = {
        "source_name": item.extra.get("source_name", item.subdomain_id),
        "one_line_summary": tags["one_line_summary"],
        "tech_tags_json": json.dumps(tags["tech_tags"], ensure_ascii=False),
        "entity_tags_json": json.dumps(tags["entity_tags"], ensure_ascii=False),
        "scenario_tags_json": json.dumps(tags["scenario_tags"], ensure_ascii=False),
        "industry_tags_json": json.dumps(tags["industry_tags"], ensure_ascii=False),
    }
    topic_row = {"representative_title": item.title}

    r2 = run_stage("打分", lambda: score_topic(topic_row, [article_row], config["modules"]))
    results.append(r2)
    _print_stage(r2)
    if not r2["ok"]:
        return results
    content_type = r2["result"]["content_type"]

    source_row = {
        "source_name": article_row["source_name"],
        "title": item.title,
        "url": item.url,
        "content": item.summary,
    }

    r3 = run_stage(
        "寫作",
        lambda: generate_topic_article(config["newsletter"]["name"], item.title, content_type, [source_row]),
    )
    results.append(r3)
    _print_stage(r3)
    if not r3["ok"]:
        return results
    article = r3["result"]

    r4 = run_stage("自檢", lambda: self_check(article, [source_row]))
    results.append(r4)
    _print_stage(r4)

    return results


def _print_stage(r: dict) -> None:
    status = "成功" if r["ok"] else f"失敗（{r['error_type']}）"
    print(f"  [{r['stage']}] {status}，耗時 {r['seconds']}s")
    if not r["ok"]:
        print(f"    {r['error']}")


def _swap_env(overrides: dict[str, str]):
    original = {k: os.environ.get(k) for k in overrides}

    class _Ctx:
        def __enter__(self):
            os.environ.update(overrides)
            return self

        def __exit__(self, *_exc):
            for k, v in original.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    return _Ctx()


def main() -> None:
    compare_groq = "--compare-groq" in sys.argv

    clawith_overrides = {
        "LLM_API_KEY": os.environ.get("CLAWITH_API_KEY", ""),
        "LLM_BASE_URL": os.environ.get("CLAWITH_BASE_URL", ""),
        "NEWSLETTER_MODEL": os.environ.get("CLAWITH_MODEL", ""),
        "NEWSLETTER_WRITING_MODEL": os.environ.get("CLAWITH_MODEL", ""),
    }
    missing = [k for k, v in clawith_overrides.items() if not v and k != "NEWSLETTER_WRITING_MODEL"]
    if missing:
        raise SystemExit(f".env 裡缺少 CLAWITH_API_KEY / CLAWITH_BASE_URL / CLAWITH_MODEL，缺：{missing}")

    all_results = {}

    with _swap_env(clawith_overrides):
        all_results["clawith"] = run_staged_test("Clawith")

    if compare_groq:
        all_results["groq"] = run_staged_test("Groq（現行設定，對照用）")

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n=== 總結 ===")
    for label, results in all_results.items():
        ok_count = sum(1 for r in results if r["ok"])
        print(f"{label}：{ok_count}/{len(results)} 個階段成功")
    print(f"\n完整結果（含每階段實際輸出）存在：{RESULTS_PATH}")
    print("每次 LLM 呼叫的原始 prompt/response 另外會存進 runs/{今天日期}/{stage}/，跟正式 pipeline 共用同一套留痕機制。")


if __name__ == "__main__":
    main()
