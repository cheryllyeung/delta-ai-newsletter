from __future__ import annotations

from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"


def render_newsletter(
    groups: list[dict],
    general_items: list[dict] | None = None,
    issue_title: str = "台達 AI 電子報",
    issue_date: str | None = None,
    intro: str = "",
) -> str:
    """把各領域的條列項目渲染成完整電子報 HTML。

    groups 結構範例：
        [{"name": "零組件", "subdomains": [{"name": "電源及系統", "entries": [...]}]}]
    entries 中每個元素是 generation.summarize.summarize_subdomain() 的輸出格式。
    """
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)
    template = env.get_template("newsletter.html.jinja")
    return template.render(
        issue_title=issue_title,
        issue_date=issue_date or date.today().isoformat(),
        intro=intro,
        general_items=general_items or [],
        groups=groups,
    )


def render_briefing(
    columns: list[dict],
    issue_title: str = "台達 AI 情報簡報",
    issue_date: str | None = None,
    intro: str = "",
) -> str:
    """把各領域的條列項目渲染成橫向情報看板 HTML（供內部網頁/連結檢視，非email格式）。

    columns 結構範例：
        [{"name": "通用AI模型趨勢", "eyebrow": "GENERAL", "is_general": True, "entries": [...]},
         {"name": "電源及系統", "eyebrow": "零組件", "is_general": False, "entries": [...]}]
    entries 中每個元素需含 title/summary/relevance/url/source_type
    （source_type 為 "research" / "news" / "community" 三選一）。
    """
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)
    template = env.get_template("briefing.html.jinja")
    return template.render(
        issue_title=issue_title,
        issue_date=issue_date or date.today().isoformat(),
        intro=intro,
        columns=columns,
    )


def save_draft(html: str, output_dir: str = "output/drafts", filename: str | None = None) -> Path:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (filename or f"{date.today().isoformat()}.html")
    out_path.write_text(html, encoding="utf-8")
    return out_path
