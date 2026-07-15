"""從 Reddit 抓取指定 subreddit 的熱門討論，作為社群實作經驗/踩雷分享的來源。

Reddit 目前對未經 OAuth 驗證的公開 JSON endpoint（.json）限制愈來愈嚴格，
實測常回傳 403。因此改用官方建議的 OAuth 方式（PRAW），需要一組免費的
「script」類型 App 憑證：https://www.reddit.com/prefs/apps

需要的環境變數：
    REDDIT_CLIENT_ID
    REDDIT_CLIENT_SECRET
    REDDIT_USER_AGENT   （建議格式："delta-ai-newsletter/0.1 by u/<your_username>"）

若未設定憑證，fetch_reddit_items() 會回傳空列表並印出提醒，不會讓整體流程中斷。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from ingestion.base import RawItem


def _get_reddit_client():
    client_id = os.environ.get("REDDIT_CLIENT_ID")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET")
    user_agent = os.environ.get("REDDIT_USER_AGENT")

    if not (client_id and client_secret and user_agent):
        print(
            "[reddit_source] 缺少 REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET / "
            "REDDIT_USER_AGENT，略過 Reddit 來源。"
        )
        return None

    import praw  # 延遲載入，避免未安裝時整個模組 import 失敗

    return praw.Reddit(
        client_id=client_id,
        client_secret=client_secret,
        user_agent=user_agent,
        check_for_async=False,
    )


def fetch_reddit_items(
    subdomain_id: str,
    subreddits: list[str],
    limit_per_subreddit: int = 10,
    min_score: int = 20,
) -> list[RawItem]:
    """抓取每個 subreddit 的熱門貼文（hot listing）。

    score 使用 Reddit 的 upvote 數，方便後續與 arXiv 結果一起依熱門程度排序。
    過濾掉 upvote 太低（min_score）的貼文以降低雜訊。
    """
    if not subreddits:
        return []

    reddit = _get_reddit_client()
    if reddit is None:
        return []

    items: list[RawItem] = []
    for subreddit_name in subreddits:
        try:
            for submission in reddit.subreddit(subreddit_name).hot(limit=limit_per_subreddit):
                if submission.stickied or submission.score < min_score:
                    continue
                items.append(
                    RawItem(
                        title=submission.title.strip(),
                        url=f"https://reddit.com{submission.permalink}",
                        source="reddit",
                        subdomain_id=subdomain_id,
                        published_at=datetime.fromtimestamp(submission.created_utc, tz=timezone.utc),
                        summary=(submission.selftext or "")[:1000],
                        score=float(submission.score),
                        extra={
                            "subreddit": subreddit_name,
                            "num_comments": submission.num_comments,
                        },
                    )
                )
        except Exception as exc:  # noqa: BLE001 - 單一 subreddit 失敗不應中斷整體流程
            print(f"[reddit_source] r/{subreddit_name} 抓取失敗：{exc}")
    return items
