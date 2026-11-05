"""근로계약서 폴더를 OCR해 [팀장명]_[이름]으로 바꾸는 간단 GUI."""

from __future__ import annotations

import queue
import os
import re
import shutil
import sys
import threading
import zipfile
import posixpath
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from license_support import device_id, load_valid_license, save_license

try:
    import pytesseract
    from PIL import Image, ImageOps
    import fitz
except ImportError as exc:
    messagebox.showerror(
        "필수 패키지 없음",
        "필수 패키지가 없습니다. 아래 명령으로 설치한 뒤 다시 실행하세요.\n\n"
        "py -m pip install -r requirements.txt\n\n"
        f"누락된 모듈: {exc.name}",
    )

if sys.platform == "win32" and not shutil.which("tesseract"):
    for candidate in (
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
    ):
        if candidate.is_file():
            pytesseract.pytesseract.tesseract_cmd = str(candidate)
            break


EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
SKIP = {
    "근로계약서", "근로자", "사용자", "사업주", "성명", "팀장명", "담당자", "확인자",
    "근로계약", "계약서", "이하", "이라", "한다", "담당공종", "근로장소", "내장공",
    "공사구간", "공사기간", "계약기간", "근로조건", "성과급", "팀단위", "갑이라", "의하",
}


def clean_korean(value: str) -> str:
    return "".join(re.findall(r"[가-힣]", value))


def find_header_top(image: Image.Image) -> int | None:
    """Find the first long table rule; scan margins vary between the PDFs."""
    gray = image.convert("L")
    pixels = gray.load()
    x0, x1 = int(image.width * 0.05), int(image.width * 0.96)
    end_y = max(1, int(image.height * 0.18))
    row_counts = [sum(1 for x in range(x0, x1) if pixels[x, y] < 70) for y in range(end_y)]
    if not row_counts:
        return None
    y = max(range(len(row_counts)), key=row_counts.__getitem__)
    # A printed horizontal table rule spans a substantial part of the page.
    if row_counts[y] < image.width * 0.20:
        return None
    return y


def _read_single_korean_name(image: Image.Image, box: tuple[int, int, int, int], *, scale: float, psm: int) -> str:
    roi = image.crop(box)
    roi = roi.resize((max(1, int(roi.width * scale)), max(1, int(roi.height * scale))), Image.Resampling.LANCZOS)
    text = pytesseract.image_to_string(roi, lang="kor+eng", config=f"--psm {psm}")
    candidates = [name for name in re.findall(r"[가-힣]{2,5}", text) if name not in SKIP and not name.endswith("팀")]
    unique = list(dict.fromkeys(candidates))
    return unique[0] if len(unique) == 1 else ""


def extract_portrait_header(image: Image.Image) -> tuple[str, str]:
    """Read the contract party and leader cells relative to the detected table."""
    top = find_header_top(image)
    if top is None:
        return "", ""
    w, h = image.size

    # Try the name cell on its own first. The second, wider crop handles scans
    # where the printed name shifts slightly to the left or right.
    user = ""
    user_boxes = (
        (int(w * 0.46), max(0, top - 1), int(w * 0.68), min(h, top + 60), 4, 11),
        # PSM 7 at 1.6x reads faint or slightly shifted names more reliably.
        (int(w * 0.33), max(0, top - 8), int(w * 0.74), min(h, top + 66), 1.6, 7),
        (int(w * 0.33), max(0, top - 8), int(w * 0.74), min(h, top + 66), 2, 11),
    )
    for x0, y0, x1, y1, scale, psm in user_boxes:
        user = _read_single_korean_name(image, (x0, y0, x1, y1), scale=scale, psm=psm)
        if user:
            break

    # The leader's value is in the far-right table cell. Two nearby strips
    # cover the row even when the page has a different top margin or scale.
    leader_hits: list[str] = []
    for offset in (110, 125):
        y0 = top + offset
        y1 = min(h, y0 + 54)
        if y1 - y0 < 20:
            continue
        roi = image.crop((int(w * 0.64), y0, int(w * 0.97), y1))
        roi = roi.resize((roi.width * 3, roi.height * 3), Image.Resampling.LANCZOS)
        text = pytesseract.image_to_string(roi, lang="kor+eng", config="--psm 7")
        leader_hits.extend(re.findall(r"[가-힣]{2,5}팀", text))
    leaders = list(dict.fromkeys(leader_hits))
    leader = leaders[0] if len(leaders) == 1 else ""
    return leader, user


def prepare_image(image: Image.Image) -> Image.Image:
    image = ImageOps.exif_transpose(image).convert("RGB")
    if image.width < 1200:
        scale = min(2.0, 1200 / image.width)
        image = image.resize((int(image.width * scale), int(image.height * scale)))
    return image


def extract_fields_from_image(image: Image.Image) -> tuple[str, str]:
    """Read only the two labeled positions, using OCR boxes and table geometry."""
    image = ImageOps.exif_transpose(image).convert("RGB")
    focused_user = focused_leader = ""
    # The supplied scans have varying top margins. Locate the first table rule
    # and read the two header cells relative to it instead of using fixed page
    # coordinates that can land on a neighboring field.
    if image.height > image.width * 1.2:
        focused_leader, focused_user = extract_portrait_header(image)
        if focused_user and focused_leader:
            return focused_leader, focused_user

    # Landscape/cropped scans use OCR boxes to reunite words on the same row.
    # Full-page portrait scans fall back here only if a focused crop is unclear.
    if image.height > image.width * 1.2:
        image = image.crop((0, 0, image.width, int(image.height * 0.5)))
    data = pytesseract.image_to_data(
        prepare_image(image), lang="kor+eng", config="--psm 6",
        output_type=pytesseract.Output.DICT,
    )
    words: list[dict[str, object]] = []
    for i, raw in enumerate(data["text"]):
        token = str(raw).strip()
        if not token:
            continue
        top = int(data["top"][i])
        height = max(1, int(data["height"][i]))
        words.append({
            "text": token,
            "left": int(data["left"][i]),
            "right": int(data["left"][i]) + int(data["width"][i]),
            "cy": top + height / 2,
            "height": height,
        })

    # Tesseract sometimes puts the table label and its value in different
    # layout blocks. Rejoin words on the same visual baseline by their boxes.
    rows: list[list[dict[str, object]]] = []
    for word in sorted(words, key=lambda w: float(w["cy"])):
        nearest = None
        nearest_gap = float("inf")
        for row in rows:
            center = sum(float(w["cy"]) for w in row) / len(row)
            gap = abs(float(word["cy"]) - center)
            tolerance = max(6.0, min(float(word["height"]), sum(float(w["height"]) for w in row) / len(row)) * 0.7)
            if gap <= tolerance and gap < nearest_gap:
                nearest, nearest_gap = row, gap
        if nearest is None:
            rows.append([word])
        else:
            nearest.append(word)
    for row in rows:
        row.sort(key=lambda word: int(word["left"]))

    leader_values: list[str] = []
    user_values: list[str] = []
    for row in rows:
        tokens = [str(word["text"]) for word in row]
        compact = [clean_korean(token) for token in tokens]

        # The table label and its value are on the same horizontal row; the
        # leader's value is in the cell to the right of the "팀장명" label.
        for start in range(len(tokens)):
            label = ""
            end = start
            while end < len(tokens) and end < start + 4 and len(label) < 5:
                label += compact[end]
                if label == "팀장명":
                    value = "".join(compact[end + 1:])
                    candidates = [n for n in re.findall(r"[가-힣]{2,6}", value) if n not in SKIP]
                    if len(candidates) == 1:
                        leader_values.append(candidates[0])
                    break
                end += 1

        # In the contract party line, the employer/user name follows
        # '(이하 갑이라 한다)와' and ends before '(이하 을이라 한다)'.
        for i, token in enumerate(compact):
            if token != "와" or not any("갑" in t for t in compact[max(0, i - 7):i]):
                continue
            end = next((j for j in range(i + 1, len(tokens)) if "이하" in tokens[j]), None)
            if end is None:
                continue
            name_text = "".join(compact[i + 1:end])
            candidates = [n for n in re.findall(r"[가-힣]{2,5}", name_text) if n not in SKIP]
            if len(candidates) == 1:
                user_values.append(candidates[0])

    leader_set = set(leader_values)
    user_set = set(user_values)
    leader = focused_leader or (next(iter(leader_set)) if len(leader_set) == 1 else "")
    user = focused_user or (next(iter(user_set)) if len(user_set) == 1 else "")
    return leader, user


def file_fields(path: Path) -> tuple[str, str]:
    page_fields: list[tuple[str, str]] = []
    if path.suffix.lower() == ".pdf":
        with fitz.open(path) as doc:
            for page in doc:
                pix = page.get_pixmap(matrix=fitz.Matrix(220 / 72, 220 / 72), alpha=False)
                page_fields.append(extract_fields_from_image(Image.frombytes("RGB", (pix.width, pix.height), pix.samples)))
    else:
        with Image.open(path) as image:
            page_fields.append(extract_fields_from_image(image))
    leaders = {leader for leader, _ in page_fields if leader}
    users = {user for _, user in page_fields if user}
    return (next(iter(leaders)) if len(leaders) == 1 else "", next(iter(users)) if len(users) == 1 else "")


def normalize_validation_name(value: object, *, is_team: bool = False) -> str:
    text = re.sub(r"\s+", "", str(value or ""))
    if is_team:
        text = re.sub(r"팀$", "", text)
    return text


def _excel_cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{ns}t"))
    value_node = cell.find(f"{ns}v")
    raw = value_node.text if value_node is not None and value_node.text else ""
    if cell_type == "s" and raw:
        try:
            return shared_strings[int(raw)]
        except (ValueError, IndexError):
            return ""
    return raw


def read_excel_pairs(path: Path) -> list[tuple[str, str]]:
    """Read team/user pairs from columns D and H of the 전체노임자료 sheet."""
    main_ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    rel_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    package_rel_ns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    with zipfile.ZipFile(path) as workbook_zip:
        workbook = ET.fromstring(workbook_zip.read("xl/workbook.xml"))
        sheets = workbook.find(f"{main_ns}sheets")
        target_sheet = next(
            (sheet for sheet in sheets.findall(f"{main_ns}sheet")
             if sheet.attrib.get("name") == "전체노임자료"),
            None,
        ) if sheets is not None else None
        if target_sheet is None:
            raise ValueError("'전체노임자료' 시트를 찾을 수 없습니다.")
        relationship_id = target_sheet.attrib.get(f"{rel_ns}id", "")
        relationships = ET.fromstring(workbook_zip.read("xl/_rels/workbook.xml.rels"))
        relationship = next(
            (item for item in relationships.findall(f"{package_rel_ns}Relationship")
             if item.attrib.get("Id") == relationship_id),
            None,
        )
        if relationship is None:
            raise ValueError("'전체노임자료' 시트 경로를 찾을 수 없습니다.")
        target = relationship.attrib.get("Target", "")
        sheet_path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))

        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in workbook_zip.namelist():
            root = ET.fromstring(workbook_zip.read("xl/sharedStrings.xml"))
            shared_strings = [
                "".join(node.text or "" for node in item.iter(f"{main_ns}t"))
                for item in root.findall(f"{main_ns}si")
            ]
        sheet_root = ET.fromstring(workbook_zip.read(sheet_path))
        sheet_data = sheet_root.find(f"{main_ns}sheetData")
        if sheet_data is None:
            return []

        pairs: list[tuple[str, str]] = []
        for row in sheet_data.findall(f"{main_ns}row"):
            values = {"D": "", "H": ""}
            for cell in row.findall(f"{main_ns}c"):
                match = re.match(r"([A-Z]+)", cell.attrib.get("r", ""))
                if match and match.group(1) in values:
                    values[match.group(1)] = _excel_cell_value(cell, shared_strings).strip()
            team = normalize_validation_name(values["D"], is_team=True)
            user = normalize_validation_name(values["H"])
            if not team or not user:
                continue
            if team in {"팀장명", "팀장"} and user in {"사용자명", "사용자", "이름"}:
                continue
            pairs.append((team, user))
        return pairs


def filename_pair(path: Path) -> tuple[str, str] | None:
    stem = path.stem
    prefix = "재확인필요_"
    if stem.startswith(prefix):
        stem = stem[len(prefix):]
    stem = re.sub(r"_\d+$", "", stem)
    if "_" not in stem:
        return None
    team, user = stem.split("_", 1)
    pair = (normalize_validation_name(team, is_team=True), normalize_validation_name(user))
    return pair if all(pair) else None


def ocr_setup_problem() -> str:
    command = pytesseract.pytesseract.tesseract_cmd
    executable = command if Path(command).is_file() else shutil.which(command)
    if not executable:
        return "Tesseract OCR가 설치되지 않았습니다.\n\nTesseract를 설치할 때 한국어 언어 데이터(kor)를 포함해 주세요.\nhttps://github.com/UB-Mannheim/tesseract/wiki"
    try:
        languages = pytesseract.get_languages(config="")
    except Exception as exc:
        return f"Tesseract OCR을 실행할 수 없습니다.\n\n{exc}"
    if "kor" not in languages:
        return "Tesseract 한국어 언어 데이터(kor)가 없습니다. Tesseract 설치 프로그램에서 한국어를 추가해 주세요."
    return ""


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("근로계약서 파일명 변경")
        icon_base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        icon_file = icon_base / "contract_renamer.ico"
        if icon_file.is_file():
            try:
                self.iconbitmap(str(icon_file))
            except tk.TclError:
                pass
        self.geometry("780x590")
        self.minsize(600, 440)
        self.folder: Path | None = None
        self.files: list[Path] = []
        self.events: queue.Queue = queue.Queue()
        self.finished_indices: set[int] = set()
        self.stop_requested = threading.Event()
        self.is_processing = False
        self.validation_pending = False
        self.log_lines: list[str] = []
        self.log_path: Path | None = None
        self.excel_path: Path | None = None
        self.expected_pairs: list[tuple[str, str]] = []
        active_license = load_valid_license()
        self.license_expiry = active_license[1] if active_license else None
        if not active_license:
            self.withdraw()
            if not self.prompt_activation():
                self.destroy()
                return
            self.deiconify()

        style = ttk.Style(self)
        style.configure("Treeview", font=("Malgun Gothic", 10), rowheight=28)
        style.configure("Treeview.Heading", font=("Malgun Gothic", 10, "bold"))

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=10, pady=10)
        self.work_tab = ttk.Frame(self.tabs, padding=8)
        self.log_tab = ttk.Frame(self.tabs, padding=8)
        self.validation_tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(self.work_tab, text="파일명 변경")
        self.tabs.add(self.validation_tab, text="검증")
        self.tabs.add(self.log_tab, text="로그")
        ttk.Label(
            self,
            text="© 2026 인구강 · 무단 복제·수정·배포 금지 · necron7577@daum.net",
            anchor="center",
        ).pack(fill="x", padx=10, pady=(0, 6))

        self.folder_button = tk.Button(self.work_tab, text="스캔된 폴더 선택", command=self.choose_folder, height=2)
        self.folder_button.pack(fill="x", pady=(0, 8))
        list_frame = ttk.Frame(self.work_tab)
        list_frame.pack(fill="both", expand=True)
        self.file_list = ttk.Treeview(list_frame, columns=("before", "arrow", "after"), show="headings")
        self.file_list.heading("before", text="변경 전")
        self.file_list.heading("arrow", text="→")
        self.file_list.heading("after", text="변경 후")
        self.file_list.column("before", width=300, anchor="w")
        self.file_list.column("arrow", width=60, anchor="center", stretch=False)
        self.file_list.column("after", width=300, anchor="w")
        list_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.file_list.yview)
        self.file_list.configure(yscrollcommand=list_scroll.set)
        self.file_list.pack(side="left", fill="both", expand=True)
        list_scroll.pack(side="right", fill="y")

        progress_frame = ttk.Frame(self.work_tab)
        progress_frame.pack(fill="x", pady=(8, 2))
        self.progress_label = ttk.Label(progress_frame, text="진행 건수 / 총 건수: 0/0 (0%)", anchor="w")
        self.progress_label.pack(fill="x", pady=(0, 4))
        self.progress_bar = ttk.Progressbar(progress_frame, mode="determinate", maximum=1, value=0)
        self.progress_bar.pack(fill="x")

        self.start_button = tk.Button(self.work_tab, text="작업 시작", command=self.on_start_button, height=2)
        self.start_button.pack(fill="x", pady=(8, 0))

        self.log_path_var = tk.StringVar(value="폴더를 선택하면 EXE 옆 log 폴더에 TXT 로그를 자동 저장합니다.")
        ttk.Label(self.log_tab, textvariable=self.log_path_var, wraplength=720, justify="left").pack(fill="x", pady=(0, 8))
        log_frame = ttk.Frame(self.log_tab)
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_frame, wrap="word", state="disabled", font=("Malgun Gothic", 10))
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")
        log_buttons = ttk.Frame(self.log_tab)
        log_buttons.pack(fill="x", pady=(8, 0))
        self.open_log_button = ttk.Button(log_buttons, text="저장된 TXT 불러오기", command=self.open_saved_log)
        self.open_log_button.pack(side="left")
        self.save_log_button = ttk.Button(log_buttons, text="다른 위치에 TXT로 저장", command=self.save_log_copy)
        self.save_log_button.pack(side="right")

        ttk.Label(
            self.validation_tab,
            text="현재 폴더 파일명을 Excel '전체노임자료' 시트의 D열(팀장명)·H열(사용자명)과 비교합니다. 작업 중 검증을 누르면 현재 파일 처리 후 검증합니다.",
            wraplength=720,
            justify="left",
        ).pack(fill="x", pady=(0, 8))
        validation_controls = ttk.Frame(self.validation_tab)
        validation_controls.pack(fill="x", pady=(0, 6))
        self.excel_button = ttk.Button(validation_controls, text="Excel 목록 업로드", command=self.choose_excel)
        self.excel_button.pack(side="left")
        self.validate_button = ttk.Button(validation_controls, text="검증 시작", command=self.run_validation, state="disabled")
        self.validate_button.pack(side="right")
        self.excel_status_var = tk.StringVar(value="Excel 목록을 업로드하세요. (.xlsx 또는 .xlsm)")
        ttk.Label(self.validation_tab, textvariable=self.excel_status_var, wraplength=720, justify="left").pack(fill="x", pady=(0, 6))
        self.validation_summary_var = tk.StringVar(value="검증 결과가 여기에 표시됩니다.")
        ttk.Label(self.validation_tab, textvariable=self.validation_summary_var, anchor="w").pack(fill="x", pady=(0, 6))
        validation_frame = ttk.Frame(self.validation_tab)
        validation_frame.pack(fill="both", expand=True)
        validation_tables = ttk.Frame(validation_frame)
        self.validation_list = ttk.Treeview(
            validation_tables,
            columns=("file", "pair"),
            show="headings",
        )
        self.validation_list.heading("file", text="파일명")
        self.validation_list.heading("pair", text="팀장명 / 사용자명")
        self.validation_list.column("file", width=300, anchor="w", stretch=True)
        self.validation_list.column("pair", width=190, anchor="w", stretch=False)
        self.validation_status = ttk.Treeview(validation_tables, columns=("result",), show="headings")
        self.validation_status.heading("result", text="검증 결과")
        self.validation_status.column("result", width=175, anchor="w", stretch=False)
        self.validation_status.tag_configure("needs_review", foreground="#c00000")
        self.validation_list.pack(side="left", fill="both", expand=True)
        self.validation_status.pack(side="left", fill="both")
        self._syncing_validation_scroll = False
        validation_scroll = ttk.Scrollbar(validation_frame, orient="vertical", command=self.scroll_validation)
        self.validation_scrollbar = validation_scroll
        self.validation_list.configure(
            yscrollcommand=lambda first, last: self.sync_validation_scroll(self.validation_status, first, last)
        )
        self.validation_status.configure(
            yscrollcommand=lambda first, last: self.sync_validation_scroll(self.validation_list, first, last)
        )
        self.validation_list.bind("<Double-1>", self.open_validation_file)
        self.validation_status.bind("<Double-1>", self.open_validation_file)
        validation_scroll.pack(side="right", fill="y")
        validation_tables.pack(side="left", fill="both", expand=True)

        self.after(150, self.poll)
        self.after(60 * 60 * 1000, self.check_license_expiry)
        self.protocol("WM_DELETE_WINDOW", self.close_app)
        self.load_latest_saved_log()

    def prompt_activation(self) -> bool:
        dialog = tk.Toplevel(self)
        dialog.title("180일 사용 인증")
        dialog.resizable(False, False)
        dialog.grab_set()
        ttk.Label(dialog, text="이 프로그램은 180일마다 인증 코드가 필요합니다.\n아래 PC 인증 ID를 이메일로 보내 코드를 발급받으세요.", justify="left").pack(anchor="w", padx=14, pady=(14, 8))
        id_row = ttk.Frame(dialog)
        id_row.pack(fill="x", padx=14)
        id_var = tk.StringVar(value=device_id())
        ttk.Entry(id_row, textvariable=id_var, state="readonly", width=28).pack(side="left", fill="x", expand=True)
        ttk.Button(id_row, text="ID 복사", command=lambda: (dialog.clipboard_clear(), dialog.clipboard_append(id_var.get()))).pack(side="left", padx=(6, 0))
        ttk.Label(dialog, text="인증 코드를 붙여넣으세요:").pack(anchor="w", padx=14, pady=(10, 3))
        code_box = tk.Text(dialog, width=56, height=4, wrap="word")
        code_box.pack(fill="x", padx=14)
        ttk.Label(
            dialog,
            text="PC 인증 ID 발급 요청: necron7577@daum.net",
            foreground="#244f77",
        ).pack(anchor="w", padx=14, pady=(8, 0))
        result = {"ok": False}

        def accept() -> None:
            try:
                self.license_expiry = save_license(code_box.get("1.0", "end").strip())
                result["ok"] = True
                dialog.destroy()
            except Exception as exc:
                messagebox.showerror("인증 실패", str(exc), parent=dialog)

        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=14, pady=12)
        ttk.Button(buttons, text="인증", command=accept).pack(side="right")
        ttk.Button(buttons, text="종료", command=dialog.destroy).pack(side="right", padx=(0, 6))
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.update_idletasks()
        dialog.lift()
        dialog.focus_force()
        self.wait_window(dialog)
        return result["ok"]

    def check_license_expiry(self) -> None:
        if self.license_expiry and self.license_expiry >= datetime.now().date():
            self.after(60 * 60 * 1000, self.check_license_expiry)
            return
        messagebox.showinfo("인증 만료", "180일 사용 기간이 끝났습니다. 새 인증 코드를 입력해 주세요.", parent=self)
        if not self.prompt_activation():
            self.destroy()
            return
        self.after(60 * 60 * 1000, self.check_license_expiry)

    def scroll_validation(self, *args: object) -> None:
        self._syncing_validation_scroll = True
        try:
            self.validation_list.yview(*args)
            self.validation_status.yview(*args)
        finally:
            self._syncing_validation_scroll = False

    def sync_validation_scroll(self, other: ttk.Treeview, first: str, last: str) -> None:
        self.validation_scrollbar.set(first, last)
        if self._syncing_validation_scroll:
            return
        self._syncing_validation_scroll = True
        try:
            other.yview_moveto(first)
        finally:
            self._syncing_validation_scroll = False

    def clear_validation_results(self) -> None:
        for table in (self.validation_list, self.validation_status):
            for iid in table.get_children():
                table.delete(iid)

    def set_progress(self, completed: int, total: int) -> None:
        percent = round(completed * 100 / total) if total else 0
        self.progress_bar.configure(maximum=max(total, 1), value=min(completed, total))
        self.progress_label.configure(text=f"진행 건수 / 총 건수: {completed}/{total} ({percent}%)")

    def start_log_session(self, folder: Path) -> None:
        self.log_lines.clear()
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        log_dir = self.app_log_directory()
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            candidate = log_dir / f"작업로그_{stamp}.txt"
            suffix = 2
            while candidate.exists():
                candidate = log_dir / f"작업로그_{stamp}_{suffix}.txt"
                suffix += 1
            candidate.write_text("", encoding="utf-8")
            self.log_path = candidate
            self.log_path_var.set(f"자동 저장 TXT: {candidate}")
        except OSError as exc:
            self.log_path = None
            self.log_path_var.set(f"자동 로그 저장 경로를 만들 수 없습니다: {exc}")
        self.append_log(f"폴더 선택: {folder}")

    @staticmethod
    def app_log_directory() -> Path:
        if getattr(sys, "frozen", False):
            app_dir = Path(sys.executable).resolve().parent
        else:
            app_dir = Path(__file__).resolve().parent
        return app_dir / "log"

    def append_log(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
        self.log_lines.append(line)
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        if self.log_path is not None:
            try:
                with self.log_path.open("a", encoding="utf-8") as stream:
                    stream.write(line + "\n")
            except OSError as exc:
                self.log_path_var.set(f"자동 로그 저장 실패: {exc}")

    def save_log_copy(self) -> None:
        if not self.log_lines:
            messagebox.showinfo("로그 저장", "저장할 로그가 없습니다.")
            return
        initial_name = self.log_path.name if self.log_path else "근로계약서_작업로그.txt"
        selected = filedialog.asksaveasfilename(
            title="로그 TXT 저장",
            defaultextension=".txt",
            initialfile=initial_name,
            filetypes=(("텍스트 파일", "*.txt"),),
        )
        if not selected:
            return
        target = Path(selected)
        try:
            self.append_log(f"로그를 별도 파일로 저장: {target}")
            target.write_text("\n".join(self.log_lines) + "\n", encoding="utf-8")
            messagebox.showinfo("로그 저장", f"TXT 로그를 저장했습니다.\n\n{target}")
        except OSError as exc:
            messagebox.showerror("로그 저장 실패", str(exc))

    def open_saved_log(self) -> None:
        log_dir = self.app_log_directory()
        selected = filedialog.askopenfilename(
            title="저장된 작업 로그 불러오기",
            initialdir=str(log_dir if log_dir.is_dir() else Path.home()),
            filetypes=(("텍스트 로그", "*.txt"),),
        )
        if not selected:
            return
        source = Path(selected)
        try:
            content = source.read_text(encoding="utf-8-sig")
        except OSError as exc:
            messagebox.showerror("로그 열기 실패", str(exc))
            return
        self.log_lines = content.splitlines()
        self.log_path = None
        self.log_path_var.set(f"불러온 TXT 로그: {source}")
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        if content:
            self.log_text.insert("1.0", content if content.endswith("\n") else content + "\n")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def load_latest_saved_log(self) -> None:
        log_dir = self.app_log_directory()
        try:
            saved_logs = list(log_dir.glob("*.txt")) if log_dir.is_dir() else []
            if not saved_logs:
                self.log_path_var.set("저장된 작업 로그가 없습니다.")
                return
            latest = max(saved_logs, key=lambda path: path.stat().st_mtime)
            content = latest.read_text(encoding="utf-8-sig")
        except OSError as exc:
            self.log_path_var.set(f"최근 저장 로그를 불러오지 못했습니다: {exc}")
            return
        self.log_lines = content.splitlines()
        self.log_path = None
        self.log_path_var.set(f"최근 저장 로그: {latest}")
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        if content:
            self.log_text.insert("1.0", content if content.endswith("\n") else content + "\n")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def choose_folder(self) -> None:
        selected = filedialog.askdirectory(title="스캔된 근로계약서 폴더 선택")
        if not selected:
            return
        self.folder = Path(selected)
        self.files = sorted((p for p in self.folder.iterdir() if p.is_file() and p.suffix.lower() in EXTENSIONS), key=lambda p: p.name.lower())
        self.finished_indices.clear()
        children = self.file_list.get_children()
        if children:
            self.file_list.delete(*children)
        for i, path in enumerate(self.files):
            self.file_list.insert("", "end", iid=str(i), values=(path.name, "────▶", ""))
        if not self.files:
            self.file_list.insert("", "end", values=("PDF 또는 이미지 파일이 없습니다.", "", ""))
        self.clear_validation_results()
        self.validation_summary_var.set("폴더를 선택했습니다. 검증 시작을 눌러 Excel 목록과 비교하세요.")
        self.start_button.configure(state="normal", text="작업 시작")
        self.set_progress(0, len(self.files))
        self.start_log_session(self.folder)
        self.update_validation_controls()

    def update_validation_controls(self) -> None:
        ready = bool(self.folder and self.files and self.expected_pairs and not self.validation_pending)
        self.validate_button.configure(state="normal" if ready else "disabled")

    def choose_excel(self) -> None:
        selected = filedialog.askopenfilename(
            title="팀장명·사용자명 Excel 목록 선택",
            filetypes=(("Excel 통합 문서", "*.xlsx *.xlsm"),),
        )
        if not selected:
            return
        path = Path(selected)
        try:
            pairs = read_excel_pairs(path)
        except Exception as exc:
            messagebox.showerror(
                "Excel 읽기 실패",
                "'전체노임자료' 시트의 D열(팀장명), H열(사용자명)을 읽지 못했습니다.\n"
                "파일을 .xlsx 또는 .xlsm 형식으로 저장한 뒤 다시 선택하세요.\n\n"
                f"상세 내용: {exc}",
            )
            self.append_log(f"Excel 업로드 실패: {path} ({exc})")
            return
        if not pairs:
            messagebox.showwarning("목록 없음", "'전체노임자료' 시트 D열과 H열에 팀장명·사용자명이 입력된 행이 없습니다.")
            return
        self.excel_path = path
        self.expected_pairs = pairs
        self.excel_status_var.set(f"업로드됨: {path} / 비교 목록 {len(pairs)}건")
        self.clear_validation_results()
        self.validation_summary_var.set("검증 시작을 눌러 현재 폴더의 파일명과 비교하세요.")
        self.append_log(f"검증 Excel 업로드: {path} ({len(pairs)}건)")
        self.update_validation_controls()

    @staticmethod
    def unique_validation_target(source: Path, target: Path) -> Path:
        if target == source or not target.exists():
            return target
        number = 2
        while True:
            candidate = target.with_name(f"{target.stem}_{number}{target.suffix}")
            if not candidate.exists() and candidate != source:
                return candidate
            number += 1

    def run_validation(self) -> None:
        if not self.folder or not self.files or not self.expected_pairs:
            messagebox.showinfo("검증 준비", "폴더를 선택하고 Excel 목록을 업로드하세요.")
            return
        if self.is_processing:
            self.validation_pending = True
            self.stop_requested.set()
            self.validate_button.configure(state="disabled")
            self.start_button.configure(state="disabled", text="검증 대기 중…")
            self.validation_summary_var.set("현재 파일 처리가 끝나면 검증을 시작합니다.")
            self.append_log("검증 요청됨: 현재 파일 처리가 끝난 뒤 작업을 멈추고 검증합니다.")
            return
        self.validation_pending = False
        self.update_validation_controls()

        expected_pairs = set(self.expected_pairs)
        matches = 0
        mismatches = 0
        rename_errors = 0
        prefix = "재확인필요_"
        sort_priority: dict[str, int] = {}
        self.clear_validation_results()

        for index, source in enumerate(list(self.files)):
            pair = filename_pair(source)
            is_match = pair in expected_pairs if pair else False
            if is_match and pair is not None:
                matches += 1
                status = "일치"
                target = source
                if source.name.startswith(prefix):
                    unmarked = source.with_name(source.name[len(prefix):])
                    if unmarked.exists() and unmarked != source:
                        status = "일치 (표시 유지: 이름 중복)"
                    else:
                        target = unmarked
            else:
                mismatches += 1
                status = "Excel 목록에 없음"
                target = source if source.name.startswith(prefix) else source.with_name(prefix + source.name)
                if target != source:
                    target = self.unique_validation_target(source, target)
                    status = "재확인필요"
                else:
                    status = "유지"

            if target != source:
                try:
                    source.rename(target)
                    self.files[index] = target
                    self.append_log(f"검증 파일명 변경: {source.name} → {target.name} ({status})")
                except OSError as exc:
                    rename_errors += 1
                    target = source
                    status = f"이름 변경 실패: {exc}"
                    self.append_log(f"검증 파일명 변경 실패: {source.name} ({exc})")
            else:
                self.append_log(f"검증 {status}: {source.name}")

            pair_display = f"{pair[0]} / {pair[1]}" if pair else "파일명 형식 확인 필요"
            tags = ("needs_review",) if status in {"재확인필요", "유지"} else ()
            iid = str(index)
            if status == "재확인필요":
                sort_priority[iid] = 0
            elif status == "유지":
                sort_priority[iid] = 1
            elif status.startswith("일치"):
                sort_priority[iid] = 2
            else:
                sort_priority[iid] = 3
            self.validation_list.insert("", "end", iid=iid, values=(target.name, pair_display))
            self.validation_status.insert("", "end", iid=iid, values=(status,), tags=tags)
            if iid in self.file_list.get_children():
                old_values = self.file_list.item(iid, "values")
                if old_values:
                    self.file_list.item(iid, values=(old_values[0], "────▶", target.name))

        self.validation_summary_var.set(
            f"일치 {matches}건 / 재확인필요 {mismatches}건 / 이름 변경 실패 {rename_errors}건"
        )
        ordered_iids = sorted(sort_priority, key=lambda iid: (sort_priority[iid], int(iid)))
        for position, iid in enumerate(ordered_iids):
            self.validation_list.move(iid, "", position)
            self.validation_status.move(iid, "", position)
        self.append_log(
            f"검증 완료: 일치 {matches}건, 재확인필요 {mismatches}건, 이름 변경 실패 {rename_errors}건"
        )
        if self.start_button.cget("text") == "검증 대기 중…":
            if len(self.finished_indices) < len(self.files):
                self.start_button.configure(state="normal", text="남은 작업 시작")
            else:
                self.start_button.configure(state="normal", text="작업 검증")

    def open_validation_file(self, event: tk.Event) -> None:
        row_id = event.widget.identify_row(event.y)
        if not row_id:
            return
        try:
            index = int(row_id)
            path = self.files[index]
            if not path.is_file():
                raise FileNotFoundError(path)
            os.startfile(str(path))
        except (IndexError, ValueError, OSError) as exc:
            messagebox.showerror("파일 열기 실패", f"선택한 파일을 열 수 없습니다.\n\n{exc}")

    def on_start_button(self) -> None:
        if self.is_processing:
            self.stop_requested.set()
            self.start_button.configure(state="disabled", text="중단 요청 중…")
            self.append_log("중단 요청됨: 현재 파일 처리를 마친 뒤 멈춥니다.")
            return
        if self.start_button.cget("text") == "작업 검증":
            self.tabs.select(self.validation_tab)
            return
        self.start_work()

    def start_work(self) -> None:
        if not self.files:
            messagebox.showinfo("폴더 선택", "먼저 스캔된 폴더를 선택하세요.")
            return
        problem = ocr_setup_problem()
        if problem:
            messagebox.showerror("OCR 설치 필요", problem)
            return
        jobs = [(index, path) for index, path in enumerate(self.files) if index not in self.finished_indices]
        if not jobs:
            self.start_button.configure(state="normal", text="작업 검증")
            return
        self.stop_requested.clear()
        self.is_processing = True
        self.clear_validation_results()
        self.validation_summary_var.set("파일명 변경 작업이 진행 중입니다.")
        self.folder_button.configure(state="disabled")
        self.open_log_button.configure(state="disabled")
        self.update_validation_controls()
        self.start_button.configure(state="normal", text="작업 중단")
        self.append_log(f"작업 시작: 남은 파일 {len(jobs)}개 / 전체 {len(self.files)}개")
        thread = threading.Thread(
            target=self.process_files,
            args=(jobs, len(self.files), len(self.finished_indices)),
            daemon=True,
        )
        thread.start()

    def process_files(self, jobs: list[tuple[int, Path]], total: int, initial_done: int) -> None:
        reserved: set[Path] = set()
        unknown_number = 0
        completed = initial_done
        stopped = False
        for index, path in jobs:
            if self.stop_requested.is_set():
                stopped = True
                break
            if path.name.startswith("재확인필요_"):
                self.events.put(("updated", index, path.name, ""))
                self.events.put(("log", f"재확인필요 파일은 OCR 및 이름 변경을 건너뜀: {path.name}"))
                completed += 1
                self.events.put(("progress", completed, total))
                continue
            recognition_error = ""
            try:
                leader, user = file_fields(path)
            except Exception as exc:
                leader, user = "", ""
                recognition_error = f"OCR 오류({type(exc).__name__}): {exc}"

            leader = re.sub(r"팀$", "", leader).strip()

            if leader and user:
                stem = f"{INVALID.sub('', leader).strip()}_{INVALID.sub('', user).strip()}"
                target = path.with_name(f"{stem}{path.suffix.lower()}")
                duplicate = 2
                while (target.exists() and target != path) or target in reserved:
                    target = path.with_name(f"{stem}_{duplicate}{path.suffix.lower()}")
                    duplicate += 1
            else:
                unknown_number += 1
                while True:
                    stem = f"[인식불가]_[인식불가]{unknown_number}"
                    target = path.with_name(f"{stem}{path.suffix.lower()}")
                    if (not target.exists() or target == path) and target not in reserved:
                        break
                    unknown_number += 1

            try:
                if target != path:
                    path.rename(target)
                reserved.add(target)
                self.events.put(("updated", index, target.name, ""))
                if leader and user:
                    self.events.put(("log", f"변경 완료: {path.name} → {target.name}"))
                else:
                    self.events.put(("log", f"인식 불가 처리: {path.name} → {target.name}"))
            except Exception as exc:
                self.events.put(("updated", index, path.name, f"변경 실패: {exc}"))
                self.events.put(("log", f"파일명 변경 실패: {path.name} ({exc})"))
            if recognition_error:
                self.events.put(("log", f"{path.name}: {recognition_error}"))
            completed += 1
            self.events.put(("progress", completed, total))
        if stopped:
            self.events.put(("stopped", completed, total))
        else:
            self.events.put(("done", completed, total))

    def poll(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "updated":
                    _, index, new_name, error = event
                    iid = str(index)
                    old_values = self.file_list.item(iid, "values")
                    self.file_list.item(iid, values=(old_values[0], "────▶", error or new_name))
                    self.finished_indices.add(index)
                    if not error and index < len(self.files):
                        self.files[index] = self.files[index].with_name(new_name)
                elif event[0] == "progress":
                    self.set_progress(event[1], event[2])
                elif event[0] == "log":
                    self.append_log(event[1])
                elif event[0] == "stopped":
                    self.set_progress(event[1], event[2])
                    self.is_processing = False
                    self.folder_button.configure(state="normal")
                    self.open_log_button.configure(state="normal")
                    self.update_validation_controls()
                    if len(self.finished_indices) < len(self.files):
                        self.start_button.configure(state="normal", text="남은 작업 시작")
                        self.append_log(f"작업 중단됨: {len(self.finished_indices)}/{len(self.files)}개 처리")
                    else:
                        self.start_button.configure(state="normal", text="작업 검증")
                        self.append_log("모든 파일 처리가 완료되었습니다.")
                    if self.validation_pending:
                        self.start_button.configure(state="disabled", text="검증 대기 중…")
                        self.after(0, self.run_validation)
                elif event[0] == "done":
                    self.set_progress(event[1], event[2])
                    self.is_processing = False
                    self.folder_button.configure(state="normal")
                    self.open_log_button.configure(state="normal")
                    self.update_validation_controls()
                    if len(self.finished_indices) < len(self.files):
                        self.start_button.configure(state="normal", text="남은 작업 시작")
                        self.append_log("일부 파일이 처리되지 않았습니다. 남은 작업을 다시 시작할 수 있습니다.")
                    else:
                        self.start_button.configure(state="normal", text="작업 검증")
                        self.append_log("모든 파일 처리가 완료되었습니다.")
                    if self.validation_pending:
                        self.start_button.configure(state="disabled", text="검증 대기 중…")
                        self.after(0, self.run_validation)
        except queue.Empty:
            pass
        self.after(150, self.poll)

    def close_app(self) -> None:
        if self.is_processing:
            self.stop_requested.set()
        self.destroy()

if __name__ == "__main__":
    App().mainloop()
