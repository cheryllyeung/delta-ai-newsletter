"""產出日報意見追蹤表（xlsx），2026-09-30 加。

為什麼要這張表：意見散在各人信箱裡，提的人不知道有沒有被處理，也看不到
別人提過什麼，同一件事會被重複提。這張表放在 SharePoint 共同編輯，他們
自己開列填，我們只更新狀態欄。

設計上配合共編的兩個取捨：
- 狀態與類別用下拉選單，不讓各人自由填。之後要篩「還沒處理的」才篩得動
- 編號欄不放公式。共編時公式很容易被整列覆蓋或貼掉，手填反而穩

預設會先填入已經處理完的項目當範例。看得到「提了會動」的紀錄，人才會
願意繼續提。

用法：
    python -m tools.make_feedback_tracker
    python -m tools.make_feedback_tracker --force   # 覆蓋既有檔案

注意：檔案一旦上傳 SharePoint 就以那份為準，不要再用這支蓋回去（會吃掉
他們填的內容）。--force 只在還沒發出去前重做用。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "docs" / "基因檢測日報_意見追蹤.xlsx"

CATEGORIES = ["版面", "內容", "來源", "功能", "錯誤"]
STATUSES = ["待評估", "進行中", "已完成", "不做"]

# 欄位：標題 與 欄寬
COLUMNS = [
    ("編號", 7),
    ("提出日期", 12),
    ("提出人", 12),
    ("類別", 9),
    ("意見內容", 46),
    ("期待結果", 32),
    ("狀態", 10),
    ("處理說明", 46),
    ("完成日期", 12),
]

# 開檔時先放進去的紀錄（2026-09-30 之前實際處理過的）
SEED = [
    ("2026-09-30", "內部", "內容", "上面導讀的標題與下面報導的標題不完全相同，讀起來像兩則不同的報導",
     "上下標題與分類統一", "已完成",
     "導讀改成直接列下方報導的原標題加序號；分類統一為市場／技術／臨床／法規，不再混用廠商動態、台灣動態那套", "2026-09-30"),
    ("2026-09-30", "內部", "版面", "類別標示有些顯示紫色，不知道代表什麼意思",
     "顏色要有說明，或不要用顏色", "已完成",
     "移除每個類別一個色碼的做法，類別名稱本身已寫清楚；另外修掉 Outlook 把點過的連結套成紫色的問題", "2026-09-30"),
    ("2026-09-30", "內部", "版面", "標籤在 Outlook 裡黏成一串（CDK4MCL1CD276CDKN2ACDKN2B）",
     "標籤要能分辨", "已完成",
     "改用 #hashtag 純文字。原本的框線版依賴 Outlook 會丟掉的內距與邊界，所以標籤全部貼在一起", "2026-09-30"),
    ("2026-09-30", "內部", "內容", "主編觀察在複述上面的報導，沒有新東西",
     "要宏觀、深入的判斷與提問", "已完成",
     "改寫寫作規則：每塊必須是跨則才看得到的東西（幾則指向同一件事、互相削弱、共用未驗證前提），報導事實只能壓縮成短句當證據", "2026-09-30"),
    ("2026-09-30", "內部", "版面", "「值得追問」四個字多餘，問句本身已經看得出來",
     "直接顯示問句", "已完成", "移除前綴，問句以金色單獨呈現", "2026-09-30"),
    ("2026-09-30", "內部", "錯誤", "Thymia 那則把 80% 寫成敏感度，原文的敏感度是 82%（80% 是模型給糖尿病者較高風險分數的比例）",
     "指標名稱不可寫錯", "已完成",
     "摘要、導讀、主編觀察三處都更正；生成與自檢規則加入「指標改名視為失真」，之後自檢會擋下來", "2026-09-30"),
    ("2026-09-30", "內部", "錯誤", "Guardant Health 與慧智基因在報導裡出現過，卻沒被認成關注廠商",
     "關注廠商要抓得到", "已完成",
     "補別名：來源寫的是產品名 Guardant360 CDx，而比對規則不允許公司名後接數字；慧智基因在來源只寫「慧智」", "2026-09-30"),
    ("2026-09-30", "內部", "來源", "26 家關注廠商中有 9 家至今零覆蓋：Tempus AI、BillionToOne、Element Biosciences、Caris Life Sciences、Qiagen、Centogene、華大基因、Macrogen、Gene Solutions",
     "這些廠商的動態要抓得到", "進行中",
     "美國幾家的消息多發在自家新聞室或被訂閱制媒體獨家，計畫加各家新聞室 RSS（免費，我們自己做）；訂閱制媒體需要帳號權限；亞洲幾家要新接中文與韓文來源", ""),
    ("2026-09-30", "內部", "錯誤", "Endpoints News 這個來源接上之後一則都沒抓進來",
     "來源要正常運作", "進行中", "查 feed 設定是否有誤，或該來源是否需要登入才給全文", ""),
    ("2026-09-30", "內部", "來源", "社群討論的訊號尚未納入，目前只看媒體與期刊",
     "納入社群訊號", "待評估", "需要 Reddit 的 API 憑證；HN 在基因檢測領域的討論量偏低，實測幾乎抓不到東西", ""),
    ("2026-09-30", "內部", "功能", "焦點與主編觀察的判斷依據看不出來，讀者不知道憑什麼挑、憑什麼寫",
     "第一次寄送時說明編輯邏輯", "已完成",
     "做成一頁 PDF「日報編輯脈絡」隨信附上，信裡只在信尾提一行，不佔版面", "2026-09-30"),
]

HEADER_FILL = PatternFill("solid", fgColor="1F3A5F")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=11)
THIN = Side(style="thin", color="D9D9D9")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
STATUS_FILLS = {
    "已完成": "D9EAD3",
    "進行中": "FFF2CC",
    "待評估": "EFEFEF",
    "不做": "F4CCCC",
}


def _sheet_tracker(wb: Workbook) -> None:
    ws = wb.active
    ws.title = "意見追蹤"

    for col, (title, width) in enumerate(COLUMNS, 1):
        cell = ws.cell(row=1, column=col, value=title)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.row_dimensions[1].height = 26

    for i, row in enumerate(SEED, start=2):
        ws.cell(row=i, column=1, value=i - 1)
        for col, value in enumerate(row, start=2):
            ws.cell(row=i, column=col, value=value)

    last_row = len(SEED) + 400  # 留空白列給他們填，下拉選單一起套用
    for r in range(2, last_row + 1):
        ws.row_dimensions[r].height = 30
        for c in range(1, len(COLUMNS) + 1):
            cell = ws.cell(row=r, column=c)
            cell.border = BORDER
            cell.alignment = Alignment(vertical="top", wrap_text=c in (5, 6, 8))

    # 下拉選單：類別（D 欄）與狀態（G 欄）
    dv_cat = DataValidation(type="list", formula1=f'"{",".join(CATEGORIES)}"', allow_blank=True)
    dv_cat.error = "請從清單選一個類別"
    dv_sta = DataValidation(type="list", formula1=f'"{",".join(STATUSES)}"', allow_blank=True)
    dv_sta.error = "請從清單選一個狀態"
    ws.add_data_validation(dv_cat)
    ws.add_data_validation(dv_sta)
    dv_cat.add(f"D2:D{last_row}")
    dv_sta.add(f"G2:G{last_row}")

    # 狀態上色，一眼看出哪些還沒動
    for status, colour in STATUS_FILLS.items():
        ws.conditional_formatting.add(
            f"A2:I{last_row}",
            FormulaRule(formula=[f'$G2="{status}"'], fill=PatternFill("solid", fgColor=colour), stopIfTrue=False),
        )

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:I{last_row}"


def _sheet_howto(wb: Workbook) -> None:
    ws = wb.create_sheet("填寫說明")
    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 96
    rows = [
        ("這張表做什麼", "收集 Delta 基因檢測日報的意見並追蹤處理狀況。看到哪裡不對、想要什麼、覺得某個來源該加，直接開新的一列填。"),
        ("怎麼填", "填「提出日期、提出人、類別、意見內容、期待結果」五欄就好。狀態與處理說明由我們更新，不用填。"),
        ("", ""),
        ("類別怎麼選", ""),
        ("版面", "排版、字級、顏色、標籤顯示、在某個信件軟體裡壞掉。"),
        ("內容", "摘要或主編觀察的寫法、深度、切角、語氣。"),
        ("來源", "某個來源該加或該減、某家廠商沒被抓到、某個領域沒覆蓋。"),
        ("功能", "希望日報多做或少做某件事，例如附件、分區、頻率。"),
        ("錯誤", "事實寫錯、數字寫錯、連結壞掉、名稱拼錯。這類請盡量附上原文連結，我們才查得到。"),
        ("", ""),
        ("狀態的意思", ""),
        ("待評估", "收到了，還在判斷要不要做、怎麼做。"),
        ("進行中", "確定要做，正在改。"),
        ("已完成", "改完並且已經反映在最新一期。處理說明會寫改了什麼。"),
        ("不做", "評估後決定不做，處理說明會寫原因。"),
        ("", ""),
        ("為什麼表裡已經有內容", "那幾列是 9 月 30 日之前已經提過並處理完的，放著當範例，也讓大家看得到提了之後會怎麼被處理。"),
    ]
    for i, (a, b) in enumerate(rows, start=1):
        ws.cell(row=i, column=1, value=a).font = Font(bold=bool(a) and not b or a in ("類別怎麼選", "狀態的意思"))
        ws.cell(row=i, column=2, value=b).alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[i].height = 30 if b else 18
    ws.cell(row=1, column=1).font = Font(bold=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="覆蓋既有檔案（已發出去的表不要用）")
    parser.add_argument("--out", default=str(OUT_PATH))
    args = parser.parse_args()

    out = Path(args.out)
    if out.exists() and not args.force:
        print(f"[make_feedback_tracker] 檔案已存在，沒有覆蓋：{out}")
        print("  已經共編的表請直接在 SharePoint 上改；真要重做加 --force。")
        sys.exit(1)

    wb = Workbook()
    _sheet_tracker(wb)
    _sheet_howto(wb)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    print(f"[make_feedback_tracker] 已產出：{out}（預填 {len(SEED)} 列）")


if __name__ == "__main__":
    main()
