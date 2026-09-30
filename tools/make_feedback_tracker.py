"""產出日報回饋追蹤表（xlsx），2026-09-30 加。

為什麼要這張表：回饋散在各人信箱裡，提的人不知道有沒有被處理，也看不到
別人提過什麼，同一件事會被重複提。這張表放在 SharePoint 共同編輯，他們
自己開列填，我們只更新狀態欄。

設計上配合共編的三個取捨：
- 第一頁放填寫說明，第二頁才是表。開檔就看到怎麼填，不必有人在旁邊教
- 類型與狀態用下拉選單，不讓各人自由填。之後要篩「還沒處理的」才篩得動
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
OUT_PATH = ROOT / "docs" / "基因檢測日報_回饋追蹤.xlsx"

# 類型照 issue tracker 的講法（2026-09-30 使用者定）：壞掉的、想要的、有疑慮的
TYPES = ["BUG", "FEATURE", "CONCERN"]
STATUSES = ["待評估", "進行中", "已完成", "不做"]

COLUMNS = [
    ("編號", 7),
    ("提出日期", 12),
    ("提出人", 11),
    ("類型", 11),
    ("回饋內容", 44),
    ("期待結果", 28),
    ("狀態", 10),
    ("處理說明", 44),
    ("完成日期", 12),
]

# 開檔時先放進去的紀錄（2026-09-30 之前實際提過並處理的）
SEED = [
    {
        "date": "2026-09-30", "by": "內部", "type": "BUG",
        "what": "上面導讀的標題與下面報導的標題不完全相同，讀起來像兩則不同的報導",
        "want": "上下標題與分類統一", "status": "已完成",
        "done": "導讀改成直接列下方報導的原標題加序號；分類統一為市場／技術／臨床／法規，不再混用廠商動態、台灣動態那套",
        "when": "2026-09-30",
    },
    {
        "date": "2026-09-30", "by": "內部", "type": "BUG",
        "what": "標籤在 Outlook 裡黏成一串（CDK4MCL1CD276CDKN2ACDKN2B）",
        "want": "標籤要能分辨", "status": "已完成",
        "done": "改用 #hashtag 純文字。原本的框線版依賴 Outlook 會丟掉的內距與邊界，所以標籤全部貼在一起",
        "when": "2026-09-30",
    },
    {
        "date": "2026-09-30", "by": "內部", "type": "BUG",
        "what": "Thymia 那則把 80% 寫成敏感度，原文的敏感度是 82%（80% 是模型給糖尿病者較高風險分數的比例）",
        "want": "指標名稱不可寫錯", "status": "已完成",
        "done": "摘要、導讀、主編觀察三處都更正；生成與自檢規則加入「指標改名視為失真」，之後自檢會擋下來",
        "when": "2026-09-30",
    },
    {
        "date": "2026-09-30", "by": "內部", "type": "BUG",
        "what": "Guardant Health 與慧智基因在報導裡出現過，卻沒被認成關注廠商",
        "want": "關注廠商要抓得到", "status": "已完成",
        "done": "補別名：來源寫的是產品名 Guardant360 CDx，而比對規則不允許公司名後接數字；慧智基因在來源只寫「慧智」。全庫覆蓋從 15 家變 17 家",
        "when": "2026-09-30",
    },
    {
        "date": "2026-09-30", "by": "內部", "type": "BUG",
        "what": "Endpoints News 這個來源接上之後一則都沒抓進來",
        "want": "來源要正常運作", "status": "進行中",
        "done": "查 feed 設定是否有誤，或該來源是否需要登入才給全文", "when": "",
    },
    {
        "date": "2026-09-30", "by": "內部", "type": "CONCERN",
        "what": "類別標示有些顯示紫色，不知道代表什麼意思",
        "want": "顏色要有說明，或不要用顏色", "status": "已完成",
        "done": "移除每個類別一個色碼的做法，類別名稱本身已寫清楚；另外修掉 Outlook 把點過的連結套成紫色的問題",
        "when": "2026-09-30",
    },
    {
        "date": "2026-09-30", "by": "內部", "type": "CONCERN",
        "what": "主編觀察在複述上面的報導，沒有新東西",
        "want": "要宏觀、深入的判斷與提問", "status": "已完成",
        "done": "改寫寫作規則：每塊必須是跨則才看得到的東西（幾則指向同一件事、互相削弱、共用未驗證前提），報導事實只能壓縮成短句當證據",
        "when": "2026-09-30",
    },
    {
        "date": "2026-09-30", "by": "內部", "type": "FEATURE",
        "what": "「值得追問」四個字多餘，問句本身已經看得出來",
        "want": "直接顯示問句", "status": "已完成",
        "done": "移除前綴，問句以金色單獨呈現", "when": "2026-09-30",
    },
    {
        "date": "2026-09-30", "by": "內部", "type": "FEATURE",
        "what": "焦點與主編觀察的判斷依據看不出來，讀者不知道憑什麼挑、憑什麼寫",
        "want": "說明編輯邏輯", "status": "已完成",
        "done": "信底加五個問答（今天為什麼只有 N 則、分類怎麼分、涵蓋哪些廠商、怎麼判斷重要、主編觀察怎麼寫），數字每天現算。先前試過信裡一整框說明與另附 PDF，兩種都不好讀",
        "when": "2026-09-30",
    },
    {
        "date": "2026-09-30", "by": "內部", "type": "FEATURE",
        "what": "26 家關注廠商中有 9 家至今零覆蓋：Tempus AI、BillionToOne、Element Biosciences、Caris Life Sciences、Qiagen、Centogene、華大基因、Macrogen、Gene Solutions",
        "want": "這些廠商的動態要抓得到", "status": "進行中",
        "done": "這幾家的消息多半發在自家官方管道或被訂閱制媒體獨家拿到。下一步接社群媒體與各家官方管道（我們自己做）；訂閱制媒體要帳號權限；亞洲幾家要新接中文與韓文來源",
        "when": "",
    },
    {
        "date": "2026-09-30", "by": "內部", "type": "FEATURE",
        "what": "社群討論的訊號尚未納入，目前只看媒體與期刊",
        "want": "納入社群訊號", "status": "待評估",
        "done": "需要 Reddit 的 API 憑證；HN 在基因檢測領域的討論量偏低，實測幾乎抓不到東西", "when": "",
    },
]

INK = "1F3A5F"
GOLD = "8A5A1A"
HEADER_FILL = PatternFill("solid", fgColor=INK)
TITLE_FONT = Font(bold=True, size=14, color=INK)
HEADER_FONT = Font(bold=True, size=10.5, color="FFFFFF")
HAIR = Side(style="thin", color="C9D2DB")
BORDER = Border(left=HAIR, right=HAIR, top=HAIR, bottom=HAIR)
STATUS_FILLS = {"已完成": "DCEBD2", "進行中": "FBF0CC", "待評估": "EFF1F3", "不做": "F6D9D6"}
TYPE_FONTS = {"BUG": "B03A2E", "FEATURE": "1E6B52", "CONCERN": "8A5A1A"}
LAST_ROW = 420  # 表格範圍，下拉選單與框線都套到這裡


def _sheet_howto(wb: Workbook) -> None:
    """第一頁：怎麼填。開檔就看到，不必有人在旁邊教。"""
    ws = wb.active
    ws.title = "填寫說明"
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 3
    ws.column_dimensions["B"].width = 15
    ws.column_dimensions["C"].width = 92

    def put(row: int, label: str, text: str, *, section: bool = False, height: int = 0) -> None:
        if section:
            cell = ws.cell(row=row, column=2, value=label)
            cell.font = Font(bold=True, size=11.5, color=INK)
            ws.cell(row=row, column=3, value=text).font = Font(size=10, color="5A6672")
        else:
            cell = ws.cell(row=row, column=2, value=label)
            cell.font = Font(bold=True, size=10.5, color=GOLD)
            cell.alignment = Alignment(vertical="top")
            body = ws.cell(row=row, column=3, value=text)
            body.font = Font(size=10.5)
            body.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[row].height = height or (20 if section else 32)

    title = ws.cell(row=2, column=2, value="Delta 基因檢測日報｜回饋追蹤")
    title.font = TITLE_FONT
    ws.cell(row=3, column=2, value="看到哪裡不對、想要什麼、覺得哪裡怪，都填到第二頁「回饋追蹤」").font = Font(
        size=10.5, color="5A6672"
    )
    ws.row_dimensions[2].height = 24

    put(5, "怎麼填", "在「回饋追蹤」那頁開新的一列，填前五欄：提出日期、提出人、類型、回饋內容、期待結果。"
                     "狀態、處理說明、完成日期由我們更新，不用填。一列一件事，兩件事請開兩列，之後才追得動。", height=44)
    put(6, "填完之後", "我們每天出刊前看一次表。當天能改的當天改，改完在處理說明寫改了什麼、狀態轉「已完成」。"
                       "決定不做的也會寫原因，不會讓它留在表上沒下文。", height=44)

    put(8, "類型怎麼選", "三種就夠，不用細分", section=True)
    put(9, "BUG", "壞掉或寫錯。事實與數字寫錯、名稱拼錯、連結失效、版面在某個信件軟體裡破掉、"
                  "該抓到的廠商或來源沒抓到。這類請盡量附上原文連結或截圖，我們才查得到。", height=44)
    put(10, "FEATURE", "希望多做或改做某件事。加某個來源、加某個廠商、改分區或頻率、想看到新的欄位或整理方式。")
    put(11, "CONCERN", "對方向或品質有疑慮，但還不確定該怎麼改。例如某一欄讀起來像複述、"
                       "分類不直覺、看不懂某個標示、覺得判斷下得太快。這類最有價值，不用先想好解法。", height=44)

    put(13, "狀態的意思", "只有我們會改這一欄", section=True)
    put(14, "待評估", "收到了，還在判斷要不要做、怎麼做。")
    put(15, "進行中", "確定要做，正在改。")
    put(16, "已完成", "改完並且已經反映在最新一期，處理說明會寫改了什麼。")
    put(17, "不做", "評估後決定不做，處理說明會寫原因。")

    put(19, "為什麼表裡有東西", "前面那幾列是 9 月 30 日之前提過的，處理完的與還在做的都留著。"
                               "一方面當填寫範例，一方面讓大家看得到提了之後會發生什麼事。", height=44)

    put(21, "找不到要填哪裡", "直接回信也可以，我們幫你填進表裡，一樣看得到狀態。")


def _sheet_tracker(wb: Workbook) -> None:
    """第二頁：表本身。"""
    ws = wb.create_sheet("回饋追蹤")
    ws.sheet_view.showGridLines = False
    n_cols = len(COLUMNS)

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=n_cols)
    title = ws.cell(row=1, column=1, value="Delta 基因檢測日報｜回饋追蹤　（填前五欄即可，狀態與處理說明由我們更新）")
    title.font = TITLE_FONT
    title.alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 30

    for col, (label, width) in enumerate(COLUMNS, 1):
        cell = ws.cell(row=2, column=col, value=label)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = BORDER
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.row_dimensions[2].height = 24

    for i, item in enumerate(SEED, start=3):
        values = [
            i - 2, item["date"], item["by"], item["type"], item["what"],
            item["want"], item["status"], item["done"], item["when"],
        ]
        for col, value in enumerate(values, 1):
            ws.cell(row=i, column=col, value=value)

    wrap_cols = {5, 6, 8}
    centre_cols = {1, 2, 4, 7, 9}
    for r in range(3, LAST_ROW + 1):
        ws.row_dimensions[r].height = 34
        for c in range(1, n_cols + 1):
            cell = ws.cell(row=r, column=c)
            cell.border = BORDER
            cell.font = Font(size=10.5)
            cell.alignment = Alignment(
                vertical="top",
                wrap_text=c in wrap_cols,
                horizontal="center" if c in centre_cols else "left",
            )
        # 類型用顏色區分，掃一眼就知道哪些是壞掉的
        type_cell = ws.cell(row=r, column=4)
        if type_cell.value in TYPE_FONTS:
            type_cell.font = Font(size=10.5, bold=True, color=TYPE_FONTS[type_cell.value])

    dv_type = DataValidation(type="list", formula1=f'"{",".join(TYPES)}"', allow_blank=True, showErrorMessage=True)
    dv_type.error = "請選 BUG、FEATURE 或 CONCERN"
    dv_type.prompt = "BUG：壞掉或寫錯／FEATURE：希望多做或改做／CONCERN：對方向或品質有疑慮"
    dv_type.promptTitle = "類型"
    dv_status = DataValidation(type="list", formula1=f'"{",".join(STATUSES)}"', allow_blank=True, showErrorMessage=True)
    dv_status.error = "請選待評估、進行中、已完成或不做"
    ws.add_data_validation(dv_type)
    ws.add_data_validation(dv_status)
    dv_type.add(f"D3:D{LAST_ROW}")
    dv_status.add(f"G3:G{LAST_ROW}")

    # 狀態整列上色，一眼看出哪些還沒動
    for status, colour in STATUS_FILLS.items():
        ws.conditional_formatting.add(
            f"A3:{get_column_letter(n_cols)}{LAST_ROW}",
            FormulaRule(formula=[f'$G3="{status}"'], fill=PatternFill("solid", fgColor=colour), stopIfTrue=False),
        )

    ws.freeze_panes = "A3"
    ws.auto_filter.ref = f"A2:{get_column_letter(n_cols)}{LAST_ROW}"


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
    _sheet_howto(wb)
    _sheet_tracker(wb)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    print(f"[make_feedback_tracker] 已產出：{out}（預填 {len(SEED)} 列）")


if __name__ == "__main__":
    main()
