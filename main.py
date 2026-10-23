from __future__ import annotations

import base64
import gzip
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
import time
import webbrowser
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "SẢNH TRUYỆN"
BG = "#090707"
SIDEBAR = "#100B0A"
PANEL = "#17100F"
PANEL2 = "#201513"
INK = "#F5E9DE"
MUTED = "#B6A095"
DIM = "#806D63"
RED = "#D21F32"
RED_HOVER = "#F23848"
BRASS = "#B97848"
BORDER = "#573025"
GREEN = "#69C987"
YELLOW = "#E5B15A"
FONT = "Segoe UI"

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DRAFT_DIR = DATA_DIR / "drafts"
TRANSLATION_RAW_DRAFT = DRAFT_DIR / "translation_raw_draft.txt"
TRANSLATION_VI_DRAFT = DRAFT_DIR / "translation_vi_draft.txt"
TRANSLATION_DRAFT_META = DRAFT_DIR / "translation_session.json"
EPUB_DIR = DATA_DIR / "epub"
RAW_DIR = DATA_DIR / "raw"
TRANSLATED_DIR = DATA_DIR / "translated"
AUDIO_DIR = DATA_DIR / "audio"
TEMP_DIR = ROOT_DIR / "temp"
MODEL_CACHE_DIR = ROOT_DIR / "models" / "huggingface"
GLOSSARY_DIR = ROOT_DIR / "config"
GLOSSARY_PROFILES = {
    "Lâm Thánh": GLOSSARY_DIR / "glossary_lam_thanh.json",
    "Vớt Thi Nhân": GLOSSARY_DIR / "glossary_vot_thi_nhan.json",
    "Từ điển chung": GLOSSARY_DIR / "glossary_zh_vi.json",
}
EPUB2TXT_EXE = ROOT_DIR / "envs" / "epub2txt" / "Scripts" / "epub2txt.exe"
TRANSLATION_PYTHON = ROOT_DIR / "envs" / "translation" / "Scripts" / "python.exe"
AUDIO_PYTHON = ROOT_DIR / "envs" / "audio-tts" / "Scripts" / "python.exe"
TRANSLATION_SCRIPT = ROOT_DIR / "scripts" / "translate_hachimi.py"
CHATGPT_TRANSLATION_SCRIPT = ROOT_DIR / "scripts" / "chatgpt_translation.py"
AUDIO_WORKER_SCRIPT = ROOT_DIR / "scripts" / "audio_worker.py"
AUDIO_TTS_GENERATOR = ROOT_DIR / "vendor" / "Vietnamese-Long-Audio-TTS" / "generate_audio.py"
DOCUMENT_TO_TEXT_SCRIPT = ROOT_DIR / "scripts" / "document_to_text.py"
MERGE_AUDIO_SCRIPT = ROOT_DIR / "scripts" / "merge_audio.py"
GITHUB_AUDIO_REPO = os.environ.get(
    "NOVELSTUDIO_GITHUB_AUDIO_REPO",
    "harrytheanh254/Vietnamese-Long-Audio-TTS",
).strip()
GITHUB_AUDIO_REF = os.environ.get("NOVELSTUDIO_GITHUB_AUDIO_REF", "main").strip() or "main"
GITHUB_AUDIO_WORKFLOW = "generate_audio.yml"
GITHUB_AUDIO_MAX_ENCODED_CHARS = 58000


def split_text_for_clipboard_translation(text: str, max_chars: int = 2800) -> list[tuple[str, str]]:
    """Split at paragraph/line/sentence boundaries while preserving every source character.

    Each pair is (source_chunk, separator_after). The separator lets the translated chunks
    be joined without relying on the model to reproduce trailing source newlines.
    """
    if max_chars < 200:
        raise ValueError("max_chars debe ser >= 200")
    if not text:
        return []
    result: list[tuple[str, str]] = []
    start = 0
    size = len(text)
    while start < size:
        if size - start <= max_chars:
            result.append((text[start:], ""))
            break
        limit = min(start + max_chars, size)
        lower = min(start + max(200, max_chars // 2), limit)
        cut = None
        separator = " "
        # Prefer paragraph boundary, then a line ending, then sentence punctuation.
        for marker in ("\r\n\r\n", "\n\n", "\r\n", "\n", "\r"):
            idx = text.rfind(marker, lower, limit)
            if idx >= start and idx != -1:
                cut = idx + len(marker)
                separator = marker
                break
        if cut is None:
            punctuation = "。！？!?；;，,、"
            last = -1
            for mark in punctuation:
                last = max(last, text.rfind(mark, lower, limit))
            if last > start:
                cut = last + 1
                separator = " "
        if cut is None or cut <= start:
            cut = limit
            # Avoid cutting Windows CRLF in half.
            if cut < size and text[cut - 1:cut + 1] == "\r\n":
                cut -= 1
            if cut <= start:
                cut = limit
            separator = " "
        result.append((text[start:cut], separator))
        start = cut
    if result:
        last_chunk, _last_separator = result[-1]
        result[-1] = (last_chunk, "")
    if "".join(chunk for chunk, _separator in result) != text:
        raise AssertionError("Lỗi nội bộ: chia đoạn làm thay đổi văn bản nguồn.")
    return result


CHAPTER_HEADING_RE = re.compile(
    r"(?im)^[ \t\u3000]*("
    r"第\s*[0-9０-９零〇一二三四五六七八九十百千万两兩廿卅]+\s*(?:章|回|节|節|卷|集|部|篇)(?:[ \t\u3000:：.．、，\-—].*)?"
    r"|(?:Chapter\s+|Ch\u01b0\u01a1ng\s+(?:th\u1ee9\s+)?)[0-9\uff10-\uff19\u96f6\u3007\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343\u4e07]+(?:[ \t\u3000:\uff1a.\uff0e\u3001\uff0c\-\u2014].*)?"
    r")[ \t\u3000\r]*$"
)


def split_novel_into_chapters(text: str) -> tuple[list[str], list[str]]:
    """Return chapter blocks and their visible headings without changing source text."""
    if not text.strip():
        return [], []
    matches = list(CHAPTER_HEADING_RE.finditer(text))
    if not matches:
        raise ValueError(
            "Không nhận ra tiêu đề chương trong file TXT. Hãy giữ tiêu đề dạng '第1章', '第十二章', "
            "'Chapter 1' hoặc 'Chương 1' để SẢNH TRUYỆN gom chính xác 3 chương mỗi lượt."
        )
    starts = [0] + [match.start() for match in matches[1:]]
    chapters: list[str] = []
    labels: list[str] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(text)
        chapters.append(text[start:end])
        labels.append(match_label(matches[idx].group(1)))
    if "".join(chapters) != text:
        raise AssertionError("Lỗi nội bộ: tách chương làm thay đổi văn bản nguồn.")
    return chapters, labels


def match_label(value: str) -> str:
    return re.sub(r"[ \t\u3000]+", " ", value.strip())


def split_novel_into_batches(text: str, chapters_per_batch: int = 3) -> tuple[int, list[str]]:
    """Group a chaptered novel into exact batches while preserving every source character."""
    if chapters_per_batch < 1:
        raise ValueError("chapters_per_batch phải lớn hơn 0.")
    chapters, _labels = split_novel_into_chapters(text)
    batches = [
        "".join(chapters[idx:idx + chapters_per_batch])
        for idx in range(0, len(chapters), chapters_per_batch)
    ]
    return len(chapters), batches


def _matching_glossary_lines(source_chunk: str, glossary: dict[str, str], mode: str) -> str:
    matches = []
    for source, target in glossary.items():
        if source in source_chunk or target in source_chunk:
            matches.append((source, target))
    matches.sort(key=lambda item: len(item[0]), reverse=True)
    lines = "\n".join(f"{source} → {target}" for source, target in matches[:120])
    return lines or "(Không có mục từ điển khớp trực tiếp trong đoạn này; hãy ưu tiên hồ sơ tham chiếu truyện.)"


ARTIFACT_REMOVAL_RULES = (
    "Chỉ loại bỏ các dòng/phần rõ ràng nằm ngoài nội dung truyện, ví dụ: "
    "các dấu kết chương như 本章完 / 全文完 / Hết chương; PS / P.S.; lời tác giả hoặc lời người đăng ngoài truyện; "
    "chú thích biên tập/nguồn đăng không thuộc cốt truyện; quảng cáo ứng dụng/nền tảng đọc truyện, lời mời tải app, "
    "câu xin vote/xin vé tháng đặt ở cuối chương, link/quảng bá trang web; dấu nguồn như Nguồn: EbookTruyen.VN; "
    "vạch trang trí, chuỗi ký tự rác hoặc ký hiệu thừa. Không dịch/viết lại các phần rác đó. "
    "Không xóa tiêu đề chương, lời thoại, chi tiết cốt truyện hoặc văn bản có ý nghĩa trong truyện chỉ vì nó trông lạ. "
    "Nếu không chắc một phần có phải quảng cáo/chú thích ngoài truyện hay không, ưu tiên giữ nội dung truyện."
)


def build_story_research_prompt(
    novel_title: str,
    source_mode: str,
    source_text: str,
    glossary: dict[str, str],
) -> str:
    """Build a reference-research prompt from chapter headings and short previews."""
    chapters, labels = split_novel_into_chapters(source_text)
    previews: list[str] = []
    for index, chapter in enumerate(chapters[:30]):
        clean = chapter.strip()
        if not clean:
            preview = "(Chương không có phần văn bản đọc được.)"
        else:
            opening = clean[:620]
            tail = "\n[…phần giữa lược khỏi bản xem trước, không phải nội dung cần dịch…]\n" + clean[-180:] if len(clean) > 850 else ""
            preview = opening + tail
        previews.append(f"[{index + 1}] TIÊU ĐỀ: {labels[index]}\n{preview}")
    glossary_text = "\n".join(f"{source} → {target}" for source, target in list(glossary.items())[:160])
    if not glossary_text:
        glossary_text = "(Chưa có từ điển riêng.)"
    title = novel_title.strip() or "Chưa xác định; hãy suy ra thận trọng từ các tiêu đề và trích đoạn."
    input_mode_description = (
        "RAW TIẾNG TRUNG — sẽ được dịch sang tiếng Việt."
        if source_mode == "Dịch raw Trung"
        else "BẢN CONVERT TIẾNG VIỆT — sẽ được biên tập/viết lại cho tự nhiên."
    )
    preview_text = "\n\n".join(previews)
    if len(chapters) > 30:
        preview_text += f"\n\n(Còn {len(chapters) - 30} chương nữa không hiện mẫu ở bước nghiên cứu; chúng sẽ được cung cấp theo từng lượt sau.)"
    return (
        "BẠN ĐANG Ở BƯỚC CHUẨN BỊ HỒ SƠ TRUYỆN, CHƯA PHẢI BƯỚC DỊCH HOẶC VIẾT LẠI.\n"
        "Hãy tìm hiểu bộ truyện để chuẩn bị dữ liệu tham chiếu nhất quán cho các yêu cầu dịch/biên tập tiếp theo trong CHÍNH CUỘC TRÒ CHUYỆN NÀY. "
        "Nếu bạn có khả năng tìm kiếm web, hãy dùng tên truyện và thông tin có thể kiểm chứng để đối chiếu; nếu không, hãy chỉ suy luận từ tiêu đề, trích đoạn và từ điển được cung cấp. "
        "Tuyệt đối không bịa thông tin canon; phân biệt dữ kiện chắc chắn, suy luận và điểm còn chưa rõ. Khi dữ liệu trong truyện mâu thuẫn với thông tin ngoài, ưu tiên văn bản người dùng cung cấp.\n\n"
        f"TÊN TRUYỆN/FILE: {title}\n"
        f"KIỂU XỬ LÝ SẮP DÙNG: {input_mode_description}\n"
        "MỤC TIÊU: lập một HỒ SƠ THAM CHIẾU GỌN NHƯNG HỮU ÍCH gồm: "
        "(1) nhân vật chính/phụ: tên gốc, cách viết tiếng Việt đề xuất, biệt danh/biến thể tên, quan hệ và vai trò đã xác nhận; "
        "(2) đại từ xưng hô, cấp bậc, cách gọi giữa các nhân vật; "
        "(3) địa danh, môn phái, tổ chức, chức danh, bảo vật, thuật ngữ và cách chuyển ngữ thống nhất; "
        "(4) bối cảnh, sự kiện và quan hệ cần nhớ để không nhầm ở các chương tiếp theo; "
        "(5) quy tắc văn phong phù hợp với thể loại; "
        "(6) danh sách điểm chưa chắc chắn cần kiểm tra lại khi có ngữ cảnh mới. "
        "Với mỗi tên/thuật ngữ quan trọng, đưa ra một cách viết tiếng Việt chuẩn và giữ nguyên cách đó trong các lượt sau. "
        "Không cần lập danh sách mọi từ thông thường.\n"
        "Đây chỉ là tài liệu chuẩn bị, KHÔNG được dịch hay viết lại các trích đoạn bên dưới. Không kể lại/tóm tắt truyện dài dòng.\n\n"
        "TỪ ĐIỂN ĐÃ CÓ (nếu có):\n" + glossary_text + "\n\n"
        "TIÊU ĐỀ VÀ TRÍCH ĐOẠN MẪU TỪ FILE NGUỒN (chỉ để nhận diện truyện và chuẩn bị dữ liệu):\n"
        + preview_text + "\n\n"
        "Hãy trả về hồ sơ tham chiếu có cấu trúc, ưu tiên tên gốc ↔ tên tiếng Việt ↔ vai trò/quan hệ và thuật ngữ. "
        "Ở các tin nhắn tiếp theo, khi người dùng gửi từng lô chương, hãy dùng hồ sơ này làm dữ liệu nhất quán, cập nhật khi văn bản mới xác nhận điều gì đó, nhưng không tự đưa hồ sơ hay phần đã dịch trước vào bản dịch. "
        "Chỉ trả về hồ sơ tham chiếu, không chào hỏi, không dịch mẫu."
    )


def build_clipboard_translation_prompt(
    source_chunk: str,
    glossary: dict[str, str],
    previous_translation: str = "",
    story_context: str = "",
    source_mode: str = "Dịch raw Trung",
    novel_title: str = "",
    batch_number: int = 1,
    batch_total: int = 1,
) -> str:
    glossary_lines = _matching_glossary_lines(source_chunk, glossary, source_mode)
    title_text = novel_title.strip() or "chưa đặt tên"
    previous_context = ""
    if previous_translation.strip():
        previous_context = (
            "NGỮ CẢNH RẤT NGẮN TỪ BẢN DỊCH TRƯỚC (chỉ dùng để hiểu mạch và xưng hô; TUYỆT ĐỐI KHÔNG lặp lại hoặc dịch lại phần này):\n"
            + previous_translation[-1200:].strip() + "\n\n"
        )
    reference_context = ""
    if story_context.strip():
        reference_context = (
            "HỒ SƠ THAM CHIẾU TRUYỆN DO BẠN LẬP Ở BƯỚC CHUẨN BỊ (dùng thống nhất tên/thuật ngữ; không chép hồ sơ vào bản dịch):\n"
            + story_context.strip()[:24000] + "\n\n"
        )
    common_rules = (
        f"Tên truyện: {title_text}. Đây là lượt {batch_number}/{batch_total} trong một đợt xử lý.\n"
        "Bạn phải làm việc như một biên dịch viên/biên tập viên tiểu thuyết chuyên nghiệp, viết ra văn xuôi tiếng Việt tự nhiên, mạch lạc, trau chuốt như người thật làm; không bám máy móc vào trật tự câu của bản máy dịch/tiếng Trung.\n"
        "CHỈ xử lý phần văn bản trong khối NGUYÊN VĂN CẦN XỬ LÝ ở cuối prompt này. Không lấy lại, không nhắc lại, không viết tiếp, không dịch lại bất kỳ chương/đoạn nào đã xuất hiện ở tin nhắn trước. Không bịa thêm, không suy diễn, không tự bổ sung tình tiết; cũng tuyệt đối không cắt bớt, tóm tắt hay lược bỏ chi tiết có trong nội dung truyện.\n"
        "Giữ nguyên thứ tự chương, tiêu đề chương có ý nghĩa, bố cục và ranh giới đoạn gần với bản gốc; không chia thành nhiều dòng vụn không cần thiết. Không dùng chữ đậm, không bọc trong Markdown/code block, không có lời dẫn, lời kết, nhận xét, giải thích hay ghi chú của bạn. Chỉ xuất nội dung cuối cùng đã dịch/biên tập.\n"
        + ARTIFACT_REMOVAL_RULES + "\n\n"
        "TỪ ĐIỂN TRUNG → VIỆT / TÊN THUẬT NGỮ KHỚP ĐOẠN NÀY:\n"
        + glossary_lines + "\n\n"
        + reference_context
        + previous_context
    )
    if source_mode == "Viết lại convert":
        task = (
            "NHIỆM VỤ: VIẾT LẠI BẢN CONVERT TIẾNG VIỆT.\n"
            "Đầu vào đã là bản convert/ dịch thô tiếng Việt; KHÔNG dịch lại từ tiếng Trung và không biến thành một bản tóm tắt. "
            "Hãy biên tập lại toàn bộ phần được gửi thành văn xuôi tiểu thuyết Việt tự nhiên, sửa trật tự câu, cách dùng từ, ngữ pháp, lời thoại và nhịp kể đang cứng/lủng củng; "
            "giữ nguyên ngôi kể, sắc thái, quan hệ, thông tin và mức độ chi tiết. Không tự ý thay đổi tính cách nhân vật hay cách xưng hô đã được xác lập.\n"
            "Bản convert có thể chứa chuỗi rác, dấu phân cách trang trí, watermark, nguồn ebook, quảng cáo, PS, chú thích ngoài truyện hoặc chữ như 'Hết chương' ở cuối; bỏ những phần ngoài truyện theo quy tắc bên trên. "
            "Không xóa tiêu đề chương hay câu văn thật sự thuộc nội dung truyện. Không in đậm bất cứ phần nào.\n"
        )
    else:
        task = (
            "NHIỆM VỤ: DỊCH RAW TIẾNG TRUNG SANG TIẾNG VIỆT.\n"
            "Dịch đầy đủ và chính xác ý nghĩa của nguyên văn tiếng Trung thành văn xuôi tiểu thuyết Việt tự nhiên, trau chuốt. "
            "Không dịch word-by-word hoặc giữ trật tự câu Trung nếu khiến tiếng Việt gượng; nhưng không đổi nội dung, góc nhìn, sắc thái, thông tin hay mức độ chi tiết. "
            "Dùng Hán-Việt/cách gọi đã có trong hồ sơ khi phù hợp, giữ nhất quán tên nhân vật và thuật ngữ. Không rút gọn hay tóm tắt.\n"
            "Không dịch/chép các phần ngoài truyện như quảng cáo nền tảng, lời tác giả/PS, chú thích người đăng, dòng nguồn, '本章完'/ 'Hết chương' hoặc chuỗi ký tự rác theo quy tắc trên; không được nhầm chúng với tình tiết thật trong truyện. Không in đậm bất cứ phần nào.\n"
        )
    return (
        common_rules + task +
        "\nNGUYÊN VĂN CẦN XỬ LÝ — CHỈ XỬ LÝ KHỐI NÀY:\n"
        "<<<BEGIN_STORY_TEXT>>>\n" + source_chunk + "\n<<<END_STORY_TEXT>>>\n"
        "Chỉ xuất nội dung đã dịch/viết lại của khối trên; không xuất các dấu BEGIN_STORY_TEXT/END_STORY_TEXT."
    )


for folder in (EPUB_DIR, RAW_DIR, TRANSLATED_DIR, AUDIO_DIR, TEMP_DIR):
    folder.mkdir(parents=True, exist_ok=True)


class RoundedButton(tk.Canvas):
    """A simple rounded button drawn on Canvas, without third-party GUI packages."""

    def __init__(self, parent, text, command, primary=False, width=None):
        self.text = text
        self.command = command
        self.primary = primary
        self.fill = RED if primary else PANEL2
        self.hover = RED_HOVER if primary else "#2A1718"
        self.current = self.fill
        px_width = width or max(112, len(text) * 8 + 30)
        super().__init__(parent, width=px_width, height=38, bg=parent.cget("bg"),
                         highlightthickness=0, bd=0, cursor="hand2")
        self.bind("<Configure>", self.draw)
        self.bind("<Enter>", self.on_enter)
        self.bind("<Leave>", self.on_leave)
        self.bind("<Button-1>", self.on_click)
        self.draw()

    def draw(self, _event=None):
        self.delete("all")
        w = max(self.winfo_width(), int(self.cget("width")))
        h = max(self.winfo_height(), 38)
        r = h // 2
        points = [r, 1, w-r, 1, w-1, 1, w-1, r, w-1, h-r, w-1, h-1,
                  r, h-1, 1, h-1, 1, r, 1, 1]
        self.create_polygon(points, smooth=True, splinesteps=24, fill=self.current,
                            outline=RED if self.primary else BORDER, width=1)
        self.create_text(w // 2, h // 2, text=self.text, fill=INK,
                         font=(FONT, 9, "bold" if self.primary else "normal"))

    def on_enter(self, _event=None):
        self.current = self.hover
        self.draw()

    def on_leave(self, _event=None):
        self.current = self.fill
        self.draw()

    def on_click(self, _event=None):
        if callable(self.command):
            self.command()


class NovelStudioApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1180x760")
        self.root.minsize(980, 640)
        self.root.configure(bg=BG)
        self.status = tk.StringVar(value="Sẵn sàng · File được lưu trên máy bạn")
        self.raw_path: Path | None = None
        self.translation_raw_draft = ""
        self.translation_vi_draft = ""
        self.translation_draft_meta: dict = {}
        self._translation_draft_after_id: str | None = None
        self.audio_path: Path | None = None
        self.audio_paths: list[Path] = []
        self.translation_running = False
        self.audio_running = False
        self.audio_queue: list[dict] = []
        self.audio_outputs: list[Path] = []
        self.audio_history: list[dict] = []
        self.audio_list_paths: dict[int, Path] = {}
        self._audio_status_after_id: str | None = None
        self._audio_spinner_angle = 0
        self.current_page = "home"
        self._load_translation_draft()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._configure_styles()
        self._shell()
        self.show_home()

    def _configure_styles(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Dark.TCombobox", fieldbackground=PANEL2, background=PANEL2,
                        foreground=INK, arrowcolor=BRASS, padding=7)
        style.map("Dark.TCombobox", fieldbackground=[("readonly", PANEL2)],
                  foreground=[("readonly", INK)], selectbackground=[("readonly", RED)])
        style.configure("Dark.TEntry", fieldbackground=PANEL2, background=PANEL2,
                        foreground=INK, insertcolor=INK, padding=6)

    def _shell(self):
        top = tk.Frame(
            self.root, bg="#0D0908", height=58,
            highlightbackground="#3B211B", highlightthickness=1
        )
        top.pack(fill="x")
        top.pack_propagate(False)

        brand_mark = tk.Canvas(top, width=32, height=32, bg="#0D0908",
                               highlightthickness=0, bd=0)
        brand_mark.pack(side="left", padx=(18, 7), pady=12)
        brand_mark.create_oval(4, 4, 28, 28, outline=RED, width=2)
        brand_mark.create_oval(9, 9, 23, 23, outline=BRASS, width=1)
        brand_mark.create_oval(13, 13, 19, 19, fill=RED, outline="")

        tk.Label(top, text="SẢNH TRUYỆN", bg="#0D0908", fg=INK,
                 font=(FONT, 14, "bold")).pack(side="left")
        tk.Label(top, text="ĐỌC  ·  DỊCH  ·  NGHE", bg="#0D0908", fg=BRASS,
                 font=(FONT, 9, "bold")).pack(side="left", padx=18)
        tk.Label(top, text="●", bg="#0D0908", fg=GREEN,
                 font=(FONT, 10, "bold")).pack(side="right", padx=(18, 7))
        tk.Label(top, text="LOCAL WORKSPACE", bg="#0D0908", fg=MUTED,
                 font=(FONT, 8, "bold")).pack(side="right")

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True)
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1)

        side = tk.Frame(body, bg=SIDEBAR, width=192,
                        highlightbackground="#2F1C17", highlightthickness=1)
        side.grid(row=0, column=0, sticky="nsw")
        side.grid_propagate(False)
        tk.Label(side, text="THƯ VIỆN HUYỀN BÍ", bg=SIDEBAR, fg=DIM,
                 font=(FONT, 8, "bold")).pack(anchor="w", padx=18, pady=(22, 13))

        self.nav = {}
        for key, label in (
            ("home", "◉  Tổng quan"),
            ("scrape", "⇩  Cào truyện"),
            ("translate", "▤  Dịch truyện"),
            ("audio", "♫  Tạo audio"),
        ):
            btn = tk.Button(
                side, text=label, anchor="w", bd=0, padx=16, pady=11,
                bg=SIDEBAR, fg=MUTED, activebackground="#2A1718",
                activeforeground=INK, font=(FONT, 10), cursor="hand2",
                relief="flat", highlightthickness=0,
                command=lambda k=key: self.navigate(k)
            )
            btn.pack(fill="x", padx=8, pady=3)
            self.nav[key] = btn

        tk.Frame(side, bg=BORDER, height=1).pack(fill="x", padx=16, pady=15)
        tk.Label(side, text="THƯ MỤC DỮ LIỆU", bg=SIDEBAR, fg=DIM,
                 font=(FONT, 8, "bold")).pack(anchor="w", padx=17, pady=(0, 8))
        for name, folder in (
            ("EPUB", EPUB_DIR), ("RAW TXT", RAW_DIR),
            ("BẢN DỊCH", TRANSLATED_DIR), ("AUDIO", AUDIO_DIR)
        ):
            tk.Button(
                side, text="›  " + name, anchor="w", bd=0, padx=16, pady=6,
                bg=SIDEBAR, fg="#8A766C", activebackground=PANEL2,
                activeforeground=INK, font=(FONT, 8), cursor="hand2",
                relief="flat", highlightthickness=0,
                command=lambda p=folder: self.open_folder(p)
            ).pack(fill="x", padx=8)

        tk.Label(side, text="OCCULT LIBRARY\nCPU EDITION · v0.3",
                 bg=SIDEBAR, fg="#59443B", font=(FONT, 8),
                 justify="left").pack(side="bottom", anchor="w", padx=17, pady=16)

        self.content = tk.Frame(body, bg=BG)
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.grid_rowconfigure(0, weight=1)
        self.content.grid_columnconfigure(0, weight=1)

        bottom = tk.Label(
            self.root, textvariable=self.status, anchor="w",
            bg="#0D0908", fg=MUTED, padx=14, pady=7, font=(FONT, 9),
            highlightbackground="#3B211B", highlightthickness=1
        )
        bottom.pack(fill="x")

    def navigate(self, name):
        {"home": self.show_home, "scrape": self.show_scrape,
         "translate": self.show_translate, "audio": self.show_audio}.get(name, self.show_home)()

    def _select_nav(self, active):
        for key, btn in self.nav.items():
            selected = key == active
            btn.configure(
                bg="#49151D" if selected else SIDEBAR,
                fg=INK if selected else MUTED,
                activebackground="#5A1923" if selected else PANEL2,
                highlightbackground=RED if selected else SIDEBAR,
                highlightthickness=1 if selected else 0,
            )

    def _clear(self, active):
        if getattr(self, "current_page", None) == "translate":
            self._save_translation_draft()
        for child in self.content.winfo_children():
            child.destroy()
        self._select_nav(active)
        self.current_page = active
        frame = tk.Frame(self.content, bg=BG)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.grid_columnconfigure(0, weight=1)
        return frame

    def _heading(self, parent, eyebrow, title, subtitle):
        tk.Label(parent, text=eyebrow.upper(), bg=BG, fg=BRASS,
                 font=(FONT, 8, "bold")).pack(anchor="w", padx=26, pady=(22, 4))
        tk.Label(parent, text=title, bg=BG, fg=INK,
                 font=(FONT, 22, "bold")).pack(anchor="w", padx=26)
        tk.Label(parent, text=subtitle, bg=BG, fg=MUTED,
                 font=(FONT, 10)).pack(anchor="w", padx=26, pady=(5, 16))

    def _panel(self, parent, title, subtitle=None):
        frame = tk.Frame(parent, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        tk.Label(frame, text=title, bg=PANEL, fg=INK,
                 font=(FONT, 11, "bold")).pack(anchor="w", padx=16, pady=(14, 2))
        if subtitle:
            tk.Label(frame, text=subtitle, bg=PANEL, fg=MUTED,
                     font=(FONT, 9), wraplength=760, justify="left").pack(anchor="w", padx=16, pady=(0, 10))
        return frame

    def _button(self, parent, text, command, primary=False, width=None):
        b = RoundedButton(parent, text, command, primary=primary, width=width)
        b.pack(side="left", padx=(0, 8), pady=6)
        return b

    def show_home(self):
        page = self._clear("home")
        self._heading(
            page, "The occult library", "Chào mừng đến SẢNH TRUYỆN",
            "Không gian thu thập, biên tập và lưu giữ những câu chuyện của bạn."
        )

        hero = tk.Frame(
            page, bg="#120B0B",
            highlightbackground="#6A2630", highlightthickness=1
        )
        hero.pack(fill="x", padx=26, pady=(0, 14))
        hero.grid_columnconfigure(0, weight=1)

        hero_text = tk.Frame(hero, bg="#120B0B")
        hero_text.grid(row=0, column=0, sticky="nsew", padx=20, pady=16)
        tk.Label(
            hero_text, text="✧  THƯ VIỆN ĐÊM TỐI  ✧",
            bg="#120B0B", fg=BRASS, font=(FONT, 9, "bold")
        ).pack(anchor="w", pady=(0, 7))
        tk.Label(
            hero_text, text="Nơi những câu chuyện được đánh thức.",
            bg="#120B0B", fg=INK, font=(FONT, 20, "bold")
        ).pack(anchor="w")
        tk.Label(
            hero_text,
            text="Từ văn bản nguồn đến bản dịch và audiobook — trong một workspace gọn nhẹ.",
            bg="#120B0B", fg=MUTED, font=(FONT, 9), wraplength=650,
            justify="left"
        ).pack(anchor="w", pady=(5, 11))
        hero_actions = tk.Frame(hero_text, bg="#120B0B")
        hero_actions.pack(anchor="w")
        self._button(hero_actions, "TIẾP TỤC LÀM VIỆC", lambda: self.navigate("translate"), True, width=180)
        self._button(hero_actions, "MỞ THƯ MỤC DỮ LIỆU", lambda: self.open_folder(DATA_DIR), width=190)

        seal = tk.Canvas(hero, width=116, height=112, bg="#120B0B",
                         highlightthickness=0, bd=0)
        seal.grid(row=0, column=1, padx=(4, 18), pady=9)
        seal.create_oval(9, 9, 107, 107, outline="#573025", width=1)
        seal.create_oval(20, 20, 96, 96, outline=BRASS, width=1)
        seal.create_oval(34, 34, 82, 82, outline="#6A2630", width=2)
        seal.create_line(58, 14, 58, 102, fill="#573025", width=1)
        seal.create_line(14, 58, 102, 58, fill="#573025", width=1)
        seal.create_polygon(58, 30, 78, 58, 58, 86, 38, 58,
                            outline=RED, fill="", width=2)
        seal.create_oval(52, 52, 64, 64, fill=RED, outline="")

        cards = tk.Frame(page, bg=BG)
        cards.pack(fill="x", padx=26, pady=(0, 12))
        for col in range(3):
            cards.grid_columnconfigure(col, weight=1, uniform="dashboard_cards")
        card_data = [
            ("01 / COLLECT", "⇩", "CÀO TRUYỆN",
             "Thu thập EPUB và chuyển thành TXT.",
             "MỞ CÔNG CỤ", lambda: self.navigate("scrape"), False),
            ("02 / TRANSLATE", "文", "DỊCH TRUYỆN",
             "Dịch raw Trung hoặc viết lại bản convert.",
             "MỞ BẢN DỊCH", lambda: self.navigate("translate"), True),
            ("03 / LISTEN", "♫", "TẠO AUDIO",
             "Tạo MP3 dài với giọng đọc đã chọn.",
             "MỞ PHÒNG THU", lambda: self.navigate("audio"), False),
        ]
        for col, (tag, icon_text, title, desc, button, command, primary) in enumerate(card_data):
            card = tk.Frame(
                cards, bg=PANEL,
                highlightbackground=RED if primary else BORDER,
                highlightthickness=1
            )
            card.grid(
                row=0, column=col, sticky="nsew",
                padx=(0 if col == 0 else 6, 6 if col < 2 else 0), pady=3
            )
            tk.Label(card, text=tag, bg=PANEL, fg=BRASS,
                     font=(FONT, 8, "bold")).pack(anchor="w", padx=14, pady=(12, 5))
            tk.Label(card, text=icon_text, bg=PANEL, fg=RED if primary else BRASS,
                     font=(FONT, 21, "bold")).pack(anchor="w", padx=14, pady=(0, 3))
            tk.Label(card, text=title, bg=PANEL, fg=INK,
                     font=(FONT, 12, "bold")).pack(anchor="w", padx=14)
            tk.Label(card, text=desc, bg=PANEL, fg=MUTED, font=(FONT, 9),
                     wraplength=235, justify="left").pack(anchor="w", padx=14, pady=(5, 8))
            actions = tk.Frame(card, bg=PANEL)
            actions.pack(anchor="w", padx=14, pady=(0, 11))
            self._button(actions, button, command, primary=primary, width=150)

        recent = self._panel(
            page, "HỒ SƠ GẦN ĐÂY",
            "Các tệp mới nhất trong kho lưu trữ cục bộ của bạn."
        )
        recent.pack(fill="both", expand=True, padx=26, pady=(0, 14))
        recent_body = tk.Frame(recent, bg=PANEL)
        recent_body.pack(fill="both", expand=True, padx=14, pady=(0, 10))
        groups = [
            ("RAW TXT", RAW_DIR, "scrape"),
            ("BẢN DỊCH", TRANSLATED_DIR, "translate"),
            ("AUDIO", AUDIO_DIR, "audio"),
        ]
        for col in range(3):
            recent_body.grid_columnconfigure(col, weight=1, uniform="recent_files")
        for col, (label, folder, route) in enumerate(groups):
            group = tk.Frame(recent_body, bg=PANEL2, highlightbackground="#30201B", highlightthickness=1)
            group.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 6, 6 if col < 2 else 0), pady=2)
            tk.Label(group, text=label, bg=PANEL2, fg=BRASS,
                     font=(FONT, 8, "bold")).pack(anchor="w", padx=10, pady=(8, 4))
            try:
                files = sorted(
                    (item for item in folder.iterdir() if item.is_file()),
                    key=lambda item: item.stat().st_mtime, reverse=True
                )[:1]
            except OSError:
                files = []
            if files:
                newest = files[0]
                tk.Label(group, text=newest.name, bg=PANEL2, fg=INK,
                         font=(FONT, 9, "bold"), anchor="w",
                         wraplength=205, justify="left").pack(anchor="w", padx=10)
                tk.Label(group, text="Cập nhật gần đây", bg=PANEL2, fg=MUTED,
                         font=(FONT, 8)).pack(anchor="w", padx=10, pady=(2, 7))
            else:
                tk.Label(group, text="Chưa có tệp", bg=PANEL2, fg=DIM,
                         font=(FONT, 9)).pack(anchor="w", padx=10, pady=(0, 7))
            tk.Button(
                group, text="MỞ THƯ MỤC  →", anchor="w",
                command=lambda p=folder: self.open_folder(p),
                bg=PANEL2, fg="#DCC8BB", activebackground="#32151A",
                activeforeground=INK, relief="flat", bd=0, cursor="hand2",
                font=(FONT, 8, "bold")
            ).pack(fill="x", padx=8, pady=(0, 6))

    def show_scrape(self):
        page = self._clear("scrape")
        self._heading(
            page, "01 / Collect", "Cào truyện",
            "Thu thập sách điện tử và chuyển đổi thành văn bản để chuẩn bị dịch."
        )

        workspace = tk.Frame(page, bg=BG)
        workspace.pack(fill="both", expand=True, padx=26, pady=(0, 10))
        workspace.grid_rowconfigure(0, weight=1)
        workspace.grid_columnconfigure(0, weight=11, uniform="collect_panels")
        workspace.grid_columnconfigure(1, weight=9, uniform="collect_panels")

        source_panel = self._panel(
            workspace, "THU THẬP NGUỒN TRUYỆN",
            "Mở Chrome thật và dùng tiện ích WebToEpub để lưu truyện thành EPUB."
        )
        source_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 7), pady=3)
        steps = tk.Frame(source_panel, bg=PANEL)
        steps.pack(fill="x", padx=14, pady=(0, 8))
        for index, (step_title, detail) in enumerate((
            ("01  /  MỞ TRÌNH DUYỆT", "Mở Chrome và truy cập trang truyện."),
            ("02  /  THU THẬP", "Dùng WebToEpub để xuất thành EPUB."),
            ("03  /  LƯU NGUỒN", "Giữ file EPUB trong kho cục bộ."),
        )):
            line = tk.Frame(steps, bg=PANEL2, highlightbackground="#30201B", highlightthickness=1)
            line.pack(fill="x", pady=3)
            tk.Label(line, text=f"{index + 1:02d}", bg=PANEL2, fg=RED,
                     font=(FONT, 11, "bold"), width=3).pack(side="left", padx=(8, 2), pady=8)
            text_area = tk.Frame(line, bg=PANEL2)
            text_area.pack(side="left", fill="x", expand=True, padx=5, pady=6)
            tk.Label(text_area, text=step_title, bg=PANEL2, fg=INK,
                     font=(FONT, 8, "bold"), anchor="w").pack(anchor="w")
            tk.Label(text_area, text=detail, bg=PANEL2, fg=MUTED,
                     font=(FONT, 8), anchor="w", wraplength=330,
                     justify="left").pack(anchor="w", pady=(2, 0))
        source_buttons = tk.Frame(source_panel, bg=PANEL)
        source_buttons.pack(fill="x", padx=14, pady=(0, 12))
        self._button(source_buttons, "MỞ CHROME / WEBTOEPUB", self.open_chrome, True, width=190)
        self._button(source_buttons, "MỞ THƯ MỤC EPUB", lambda: self.open_folder(EPUB_DIR), width=160)

        convert_panel = self._panel(
            workspace, "CHUYỂN ĐỔI TÀI LIỆU → TXT",
            "Chuyển các định dạng tài liệu phổ biến thành TXT để dùng trong mục Dịch truyện."
        )
        convert_panel.grid(row=0, column=1, sticky="nsew", padx=(7, 0), pady=3)
        file_card = tk.Frame(convert_panel, bg="#100C0B",
                             highlightbackground=BORDER, highlightthickness=1)
        file_card.pack(fill="x", padx=14, pady=(0, 9))
        tk.Label(file_card, text="TÀI LIỆU  →  TXT", bg="#100C0B", fg=BRASS,
                 font=(FONT, 10, "bold")).pack(anchor="w", padx=12, pady=(12, 4))
        tk.Label(
            file_card,
            text="EPUB · TXT/MD · HTML · DOCX/ODT · RTF · FB2 · PDF văn bản",
            bg="#100C0B", fg=MUTED, font=(FONT, 8), wraplength=330, justify="left"
        ).pack(anchor="w", padx=12, pady=(0, 8))
        self.epub_label = tk.Label(
            file_card, text="Chưa chọn tài liệu", bg="#100C0B", fg=DIM,
            font=(FONT, 8), anchor="w", wraplength=300, justify="left"
        )
        self.epub_label.pack(fill="x", padx=12, pady=(0, 12))
        convert_buttons = tk.Frame(convert_panel, bg=PANEL)
        convert_buttons.pack(fill="x", padx=14, pady=(0, 8))
        self._button(convert_buttons, "CHỌN TÀI LIỆU & CHUYỂN TXT", self.convert_epub, True, width=210)
        self._button(convert_buttons, "MỞ RAW TXT", lambda: self.open_folder(RAW_DIR), width=125)
        tk.Label(
            convert_panel, text="File gốc không bị xóa. MOBI/AZW cần Calibre; DOC cũ cần LibreOffice; PDF scan cần OCR riêng.",
            bg=PANEL, fg=MUTED, font=(FONT, 8), wraplength=360, justify="left"
        ).pack(anchor="w", padx=14, pady=(0, 12))

        log_panel = self._panel(page, "NHẬT KÝ XỬ LÝ", "Thông báo khi mở công cụ hoặc chuyển đổi tài liệu.")
        log_panel.pack(fill="x", padx=26, pady=(0, 12))
        self.scrape_log = tk.Text(
            log_panel, height=4, bg="#0E0A09", fg=MUTED, insertbackground=INK,
            font=("Consolas", 8), relief="flat", wrap="word",
            highlightbackground="#30201B", highlightthickness=1
        )
        self.scrape_log.pack(fill="x", expand=False, padx=14, pady=(0, 12))
        self._log(self.scrape_log, "Sẵn sàng. Có thể chọn nhiều loại tài liệu; file nguồn luôn được giữ nguyên.")

    def _log(self, widget, text):
        widget.configure(state="normal")
        widget.insert("end", text.rstrip() + "\n")
        widget.see("end")
        widget.configure(state="disabled")

    def open_chrome(self):
        candidates = [
            Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
        for exe in candidates:
            if exe.is_file():
                subprocess.Popen([str(exe)])
                self.status.set("Đã mở Chrome thật.")
                return
        try:
            os.startfile("chrome")  # type: ignore[attr-defined]
            return
        except Exception:
            webbrowser.open("https://www.google.com/chrome/")
            messagebox.showwarning("Chưa tìm thấy Chrome", "Không tìm thấy chrome.exe. Hãy kiểm tra Chrome đã được cài chưa.")

    def convert_epub(self):
        supported_pattern = (
            "*.epub *.txt *.md *.markdown *.rst *.log *.csv *.json *.jsonl "
            "*.yaml *.yml *.toml *.ini *.conf *.cfg *.tsv *.html *.htm *.xhtml "
            "*.xml *.docx *.odt *.fb2 *.rtf *.pdf *.mobi *.azw *.azw3 *.doc"
        )
        selected = filedialog.askopenfilename(
            title="Chọn tài liệu cần chuyển thành TXT",
            initialdir=str(EPUB_DIR),
            filetypes=[
                ("Tài liệu được hỗ trợ", supported_pattern),
                ("Sách điện tử", "*.epub *.fb2 *.mobi *.azw *.azw3"),
                ("Văn bản và tài liệu", "*.txt *.md *.markdown *.html *.htm *.docx *.odt *.rtf *.pdf"),
                ("Tất cả tệp", "*.*"),
            ],
        )
        if not selected:
            return
        source = Path(selected)
        self.epub_label.configure(text=f"{source.name}  ·  {source.suffix.upper() or 'FILE'}", fg=INK)

        if source.suffix.lower() == ".epub":
            if not EPUB2TXT_EXE.is_file():
                messagebox.showerror(
                    "Thiếu epub2txt",
                    f"Không tìm thấy công cụ:\n{EPUB2TXT_EXE}\n\nHãy cài epub2txt vào môi trường đã chuẩn bị."
                )
                return
            self.status.set("Đang chuyển EPUB sang TXT...")
            if hasattr(self, "scrape_log"):
                self._log(self.scrape_log, f"[ĐANG CHẠY] EPUB → TXT · {source.name}")
            threading.Thread(target=self._convert_worker, args=(source,), daemon=True).start()
            return

        destination = self._unique_raw_destination(source)
        try:
            if source.suffix.lower() == ".txt" and source.resolve() == destination.resolve():
                self.root.after(0, lambda: self._conversion_succeeded(destination))
                return
        except OSError:
            pass

        if not TRANSLATION_PYTHON.is_file():
            messagebox.showerror(
                "Chưa cài môi trường chuyển đổi",
                f"Không tìm thấy Python của SẢNH TRUYỆN:\n{TRANSLATION_PYTHON}"
            )
            return
        if not DOCUMENT_TO_TEXT_SCRIPT.is_file():
            messagebox.showerror(
                "Thiếu bộ chuyển đổi",
                f"Không tìm thấy mô-đun chuyển đổi:\n{DOCUMENT_TO_TEXT_SCRIPT}"
            )
            return

        self.status.set(f"Đang chuyển {source.suffix.upper()} sang TXT...")
        if hasattr(self, "scrape_log"):
            self._log(self.scrape_log, f"[ĐANG CHẠY] {source.suffix.upper()} → TXT · {source.name}")
        threading.Thread(
            target=self._convert_document_worker,
            args=(source, destination),
            daemon=True
        ).start()

    @staticmethod
    def _unique_raw_destination(source: Path) -> Path:
        """Choose a TXT path without overwriting an existing raw file."""
        destination = RAW_DIR / f"{source.stem}.txt"
        try:
            if source.resolve() == destination.resolve():
                return destination
        except OSError:
            pass
        if not destination.exists():
            return destination
        candidate = RAW_DIR / f"{source.stem}_converted.txt"
        if not candidate.exists():
            return candidate
        index = 2
        while True:
            candidate = RAW_DIR / f"{source.stem}_converted_{index}.txt"
            if not candidate.exists():
                return candidate
            index += 1

    def _convert_document_worker(self, source: Path, destination: Path):
        try:
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8:replace"
            proc = subprocess.run(
                [
                    str(TRANSLATION_PYTHON), "-X", "utf8",
                    str(DOCUMENT_TO_TEXT_SCRIPT),
                    "--input", str(source),
                    "--output", str(destination),
                ],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=900, cwd=str(ROOT_DIR), env=env
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "Bộ chuyển đổi trả về lỗi.").strip()
                raise RuntimeError(detail)
            self.root.after(0, lambda: self._conversion_succeeded(destination))
        except Exception as exc:
            self.root.after(0, lambda detail=str(exc): self._conversion_failed(detail))

    def _convert_worker(self, epub: Path):
        try:
            proc = subprocess.run([str(EPUB2TXT_EXE), "-f", str(epub)], cwd=str(epub.parent),
                                  capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
            if proc.returncode != 0:
                raise RuntimeError((proc.stderr or proc.stdout or "epub2txt trả về lỗi").strip())
            candidates = [epub.with_suffix(".txt")]
            candidates.extend(sorted(epub.parent.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True))
            produced = next((p for p in candidates if p.is_file() and p.stat().st_mtime >= epub.stat().st_mtime - 2), None)
            if produced is None:
                raise RuntimeError("Lệnh hoàn tất nhưng không tìm thấy file TXT đầu ra cạnh EPUB.")
            destination = self._unique_raw_destination(epub)
            if produced.resolve() != destination.resolve():
                shutil.copy2(produced, destination)
            self.root.after(0, lambda: self._conversion_succeeded(destination))
        except Exception as exc:
            self.root.after(0, lambda detail=str(exc): self._conversion_failed(detail))

    def _conversion_succeeded(self, destination: Path):
        self.status.set(f"Chuyển đổi xong: {destination.name}")
        if hasattr(self, "scrape_log"):
            self._log(self.scrape_log, f"[THÀNH CÔNG] {destination}")
        messagebox.showinfo("Hoàn tất", f"Đã lưu TXT tại:\n{destination}\n\nBạn có thể mở mục Dịch truyện.")

    def _conversion_failed(self, detail: str):
        self.status.set("Chuyển đổi tài liệu thất bại.")
        if hasattr(self, "scrape_log"):
            self._log(self.scrape_log, "[LỖI] " + detail)
        messagebox.showerror("Không chuyển được EPUB", detail)

    def show_translate(self):
        page = self._clear("translate")
        self._heading(
            page, "02 / Translate", "Phòng biên dịch",
            "Dịch raw tiếng Trung hoặc viết lại bản convert, giữ nguyên quy trình xử lý hiện có."
        )

        tools = tk.Frame(page, bg=BG)
        tools.pack(fill="x", padx=26, pady=(0, 5))
        self._button(tools, "NẠP FILE RAW TXT", self.load_raw_file, True, width=150)
        self._button(tools, "MỞ BẢN DỊCH ĐÃ LƯU", lambda: self.open_folder(TRANSLATED_DIR), width=180)
        self._button(tools, "KẾT NỐI CHATGPT", self.start_chatgpt_login, width=150)
        self._button(tools, "NGẮT KẾT NỐI", self.start_chatgpt_logout, width=130)

        setup = tk.Frame(page, bg=BG)
        setup.pack(fill="x", padx=26, pady=(0, 6))

        profile = tk.Frame(setup, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        profile.pack(fill="x", pady=(0, 5))
        tk.Label(profile, text="TỪ ĐIỂN", bg=PANEL, fg=BRASS,
                 font=(FONT, 8, "bold")).pack(side="left", padx=(10, 6), pady=7)
        if not hasattr(self, "glossary_profile_var"):
            saved_profile = self.translation_draft_meta.get("glossary_profile", "Lâm Thánh")
            if saved_profile not in GLOSSARY_PROFILES:
                saved_profile = "Lâm Thánh"
            self.glossary_profile_var = tk.StringVar(value=saved_profile)
        ttk.Combobox(
            profile, textvariable=self.glossary_profile_var,
            values=("Lâm Thánh", "Vớt Thi Nhân", "Từ điển chung"),
            state="readonly", width=17, style="Dark.TCombobox"
        ).pack(side="left", padx=(0, 12), pady=4)
        tk.Label(profile, text="TÊN TRUYỆN", bg=PANEL, fg=BRASS,
                 font=(FONT, 8, "bold")).pack(side="left", padx=(4, 6))
        if not hasattr(self, "story_title_var"):
            self.story_title_var = tk.StringVar(value=str(self.translation_draft_meta.get("story_title", "")))
        ttk.Entry(profile, textvariable=self.story_title_var, width=29,
                  style="Dark.TEntry").pack(side="left", padx=(0, 10), pady=4, fill="x")
        tk.Label(profile, text="Bản nháp tự lưu trên máy", bg=PANEL, fg=MUTED,
                 font=(FONT, 8)).pack(side="right", padx=10)

        mode = tk.Frame(setup, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        mode.pack(fill="x")
        tk.Label(mode, text="BỘ MÁY DỊCH", bg=PANEL, fg=BRASS,
                 font=(FONT, 8, "bold")).pack(side="left", padx=(10, 7), pady=7)
        if not hasattr(self, "translation_mode_var"):
            saved_mode = self.translation_draft_meta.get("translation_mode", "HachimiMT-60 · Cục bộ")
            allowed_modes = ("HachimiMT-60 · Cục bộ", "ChatGPT · Dịch văn học", "ChatGPT · Qua clipboard")
            if saved_mode not in allowed_modes:
                saved_mode = "HachimiMT-60 · Cục bộ"
            self.translation_mode_var = tk.StringVar(value=saved_mode)
        ttk.Combobox(
            mode, textvariable=self.translation_mode_var,
            values=("HachimiMT-60 · Cục bộ", "ChatGPT · Dịch văn học", "ChatGPT · Qua clipboard"),
            state="readonly", width=26, style="Dark.TCombobox"
        ).pack(side="left", padx=(0, 10), pady=4)
        tk.Label(
            mode, text="Hachimi chạy cục bộ · ChatGPT cần kết nối hoặc thao tác clipboard.",
            bg=PANEL, fg=MUTED, font=(FONT, 8)
        ).pack(side="left", padx=(2, 8))

        bottom = tk.Frame(page, bg=BG)
        bottom.pack(side="bottom", fill="x", padx=26, pady=(6, 12))

        panes = tk.Frame(page, bg=BG)
        panes.pack(fill="both", expand=True, padx=26, pady=(4, 3))
        panes.grid_columnconfigure(0, weight=1, uniform="translation")
        panes.grid_columnconfigure(1, weight=1, uniform="translation")
        panes.grid_rowconfigure(2, weight=1)

        tk.Label(
            panes, text="01  /  VĂN BẢN NGUỒN", bg=BG, fg=BRASS,
            font=(FONT, 9, "bold")
        ).grid(row=0, column=0, sticky="w", pady=(0, 5))
        tk.Label(
            panes, text="02  /  BẢN DỊCH TIẾNG VIỆT", bg=BG, fg=BRASS,
            font=(FONT, 9, "bold")
        ).grid(row=0, column=1, sticky="w", padx=(10, 0), pady=(0, 5))

        if not hasattr(self, "source_mode_var"):
            saved_source_mode = self.translation_draft_meta.get("source_mode", "Dịch raw Trung")
            if saved_source_mode not in ("Dịch raw Trung", "Viết lại convert"):
                saved_source_mode = "Dịch raw Trung"
            self.source_mode_var = tk.StringVar(value=saved_source_mode)
        source_choice = tk.Frame(panes, bg=BG)
        source_choice.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        for text, value in (("Dịch raw Trung", "Dịch raw Trung"), ("Viết lại convert", "Viết lại convert")):
            tk.Radiobutton(
                source_choice, text=text, variable=self.source_mode_var, value=value,
                bg=BG, fg=INK, selectcolor="#49151D", activebackground=BG,
                activeforeground=INK, font=(FONT, 9), bd=0,
                highlightthickness=0, cursor="hand2"
            ).pack(side="left", padx=(0, 10))

        tk.Label(
            panes, text="Kết quả sẽ xuất hiện ở đây; có thể chỉnh sửa trước khi lưu.",
            bg=BG, fg=MUTED, font=(FONT, 8), anchor="w"
        ).grid(row=1, column=1, sticky="w", padx=(10, 0), pady=(0, 6))

        raw_editor = tk.Frame(panes, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        raw_editor.grid(row=2, column=0, sticky="nsew")
        raw_editor.grid_rowconfigure(0, weight=1)
        raw_editor.grid_columnconfigure(0, weight=1)
        vi_editor = tk.Frame(panes, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        vi_editor.grid(row=2, column=1, sticky="nsew", padx=(10, 0))
        vi_editor.grid_rowconfigure(0, weight=1)
        vi_editor.grid_columnconfigure(0, weight=1)

        raw_scroll = tk.Scrollbar(raw_editor, orient="vertical", bg=PANEL2,
                                  troughcolor="#100C0B", activebackground=RED)
        vi_scroll = tk.Scrollbar(vi_editor, orient="vertical", bg=PANEL2,
                                 troughcolor="#100C0B", activebackground=RED)
        self.raw_box = tk.Text(
            raw_editor, bg=PANEL, fg=INK, insertbackground=INK, wrap="word",
            undo=True, relief="flat", font=(FONT, 10), padx=12, pady=12,
            yscrollcommand=raw_scroll.set, highlightthickness=0, bd=0
        )
        self.vi_box = tk.Text(
            vi_editor, bg=PANEL, fg=INK, insertbackground=INK, wrap="word",
            undo=True, relief="flat", font=(FONT, 10), padx=12, pady=12,
            yscrollcommand=vi_scroll.set, highlightthickness=0, bd=0
        )
        raw_scroll.configure(command=self.raw_box.yview)
        vi_scroll.configure(command=self.vi_box.yview)
        self.raw_box.grid(row=0, column=0, sticky="nsew")
        raw_scroll.grid(row=0, column=1, sticky="ns")
        self.vi_box.grid(row=0, column=0, sticky="nsew")
        vi_scroll.grid(row=0, column=1, sticky="ns")

        self.raw_box.insert("1.0", self.translation_raw_draft)
        self.vi_box.insert("1.0", self.translation_vi_draft)
        self.raw_box.edit_modified(False)
        self.vi_box.edit_modified(False)
        self.raw_box.bind("<<Modified>>", self._translation_text_modified)
        self.vi_box.bind("<<Modified>>", self._translation_text_modified)

        note_text = (
            "Đã khôi phục bản nháp tự động."
            if (self.translation_raw_draft or self.translation_vi_draft)
            else "Chưa nạp file."
        )
        self.translate_note = tk.Label(
            bottom, text=note_text, bg=BG, fg=MUTED, font=(FONT, 9), anchor="w"
        )
        self.translate_note.pack(side="left", fill="x", expand=True)
        self._button(bottom, "BẮT ĐẦU DỊCH", self.start_translation, True, width=150)
        self._button(bottom, "LƯU BẢN DỊCH", self.save_translation, width=150)

    def _load_translation_draft(self):
        """Restore the last translation workspace draft from local files."""
        try:
            self.translation_raw_draft = (
                TRANSLATION_RAW_DRAFT.read_text(encoding="utf-8")
                if TRANSLATION_RAW_DRAFT.is_file() else ""
            )
            self.translation_vi_draft = (
                TRANSLATION_VI_DRAFT.read_text(encoding="utf-8")
                if TRANSLATION_VI_DRAFT.is_file() else ""
            )
            if TRANSLATION_DRAFT_META.is_file():
                loaded = json.loads(TRANSLATION_DRAFT_META.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.translation_draft_meta = loaded
            saved_path = str(self.translation_draft_meta.get("raw_path") or "").strip()
            if saved_path:
                self.raw_path = Path(saved_path)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            # Never block app startup if a draft file is incomplete/corrupt.
            self.translation_draft_meta = {}
            self.status.set(f"Không đọc được bản nháp tự động: {exc}")

    def _translation_text_modified(self, event=None):
        """Debounce draft writes while the user is typing or pasting long text."""
        try:
            widget = getattr(event, "widget", None)
            if widget is not None and widget.edit_modified():
                widget.edit_modified(False)
        except tk.TclError:
            pass
        pending = getattr(self, "_translation_draft_after_id", None)
        if pending:
            try:
                self.root.after_cancel(pending)
            except tk.TclError:
                pass
        try:
            self._translation_draft_after_id = self.root.after(650, self._save_translation_draft)
        except tk.TclError:
            self._translation_draft_after_id = None

    def _save_translation_draft(self):
        """Persist raw text, Vietnamese output, and translation choices locally."""
        self._translation_draft_after_id = None
        try:
            if hasattr(self, "raw_box") and self.raw_box.winfo_exists():
                self.translation_raw_draft = self.raw_box.get("1.0", "end-1c")
            if hasattr(self, "vi_box") and self.vi_box.winfo_exists():
                self.translation_vi_draft = self.vi_box.get("1.0", "end-1c")
        except tk.TclError:
            pass

        try:
            DRAFT_DIR.mkdir(parents=True, exist_ok=True)
            for path, value in (
                (TRANSLATION_RAW_DRAFT, self.translation_raw_draft),
                (TRANSLATION_VI_DRAFT, self.translation_vi_draft),
            ):
                temp_path = path.with_suffix(path.suffix + ".tmp")
                temp_path.write_text(value, encoding="utf-8")
                os.replace(temp_path, path)

            def value_of(attribute, fallback=""):
                variable = getattr(self, attribute, None)
                try:
                    return variable.get() if variable is not None else self.translation_draft_meta.get(attribute.removesuffix("_var"), fallback)
                except (tk.TclError, AttributeError):
                    return fallback

            metadata = {
                "raw_path": str(self.raw_path) if self.raw_path else "",
                "glossary_profile": value_of("glossary_profile_var", "Lâm Thánh"),
                "story_title": value_of("story_title_var", ""),
                "translation_mode": value_of("translation_mode_var", "HachimiMT-60 · Cục bộ"),
                "source_mode": value_of("source_mode_var", "Dịch raw Trung"),
            }
            temp_meta = TRANSLATION_DRAFT_META.with_suffix(".json.tmp")
            temp_meta.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp_meta, TRANSLATION_DRAFT_META)
            self.translation_draft_meta = metadata
        except OSError as exc:
            try:
                if hasattr(self, "status"):
                    self.status.set(f"Không lưu được bản nháp: {exc}")
            except tk.TclError:
                pass

    def _on_close(self):
        """Save pending draft edits before closing the desktop app."""
        pending = getattr(self, "_translation_draft_after_id", None)
        if pending:
            try:
                self.root.after_cancel(pending)
            except tk.TclError:
                pass
            self._translation_draft_after_id = None
        self._save_translation_draft()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def load_raw_file(self):
        path = filedialog.askopenfilename(title="Chọn raw TXT", initialdir=str(RAW_DIR),
                                          filetypes=[("Text", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8-sig")
        except Exception as exc:
            messagebox.showerror("Không đọc được TXT", str(exc)); return
        self.raw_path = Path(path)
        if hasattr(self, "story_title_var"):
            title = self.raw_path.stem
            for suffix in ("_raw", "_RAW", "_raw_txt", "_RAW_TXT"):
                if title.endswith(suffix):
                    title = title[:-len(suffix)]
            # Refresh the suggested title whenever another source file is loaded.
            # The user can then edit this field if the filename is not the actual novel title.
            self.story_title_var.set(title)
        self.raw_box.delete("1.0", "end"); self.raw_box.insert("1.0", text)
        self.vi_box.delete("1.0", "end")
        self.raw_box.edit_modified(False)
        self.vi_box.edit_modified(False)
        self.translate_note.configure(text=f"Đã nạp: {self.raw_path.name}")
        self.translation_raw_draft = text
        self.translation_vi_draft = ""
        self._save_translation_draft()
        self.status.set(f"Đã nạp raw: {self.raw_path.name}")

    def save_translation(self):
        content = self.vi_box.get("1.0", "end-1c")
        if not content.strip():
            messagebox.showwarning("Chưa có bản dịch", "Khung bản dịch đang trống."); return
        default = (self.raw_path.stem + "_vi.txt") if self.raw_path else "ban_dich_vi.txt"
        path = filedialog.asksaveasfilename(title="Lưu bản dịch", initialdir=str(TRANSLATED_DIR),
                                            initialfile=default, defaultextension=".txt",
                                            filetypes=[("Text", "*.txt")])
        if path:
            Path(path).write_text(content, encoding="utf-8")
            self.status.set(f"Đã lưu bản dịch: {Path(path).name}")

    def start_translation(self):
        if self.translation_running:
            return
        raw = self.raw_box.get("1.0", "end-1c")
        self._save_translation_draft()
        if not raw.strip():
            messagebox.showwarning("Chưa có raw", "Hãy nạp hoặc dán nội dung vào khung Raw."); return
        if not TRANSLATION_PYTHON.is_file():
            messagebox.showerror("Chưa cài bộ dịch", f"Không tìm thấy môi trường dịch:\n{TRANSLATION_PYTHON}\n\nChạy scripts\\setup_translation.ps1 trước."); return
        profile_name = self.glossary_profile_var.get()
        source_mode = self.source_mode_var.get() if hasattr(self, "source_mode_var") else "Dịch raw Trung"
        selected_translation_mode = self.translation_mode_var.get()
        if source_mode == "Viết lại convert" and not selected_translation_mode.startswith("ChatGPT · Qua clipboard"):
            messagebox.showwarning(
                "Chế độ này cần ChatGPT qua clipboard",
                "“Viết lại convert” dùng prompt biên tập văn học riêng. Hãy chọn “ChatGPT · Qua clipboard” "
                "trong mục CHẾ ĐỘ DỊCH để SẢNH TRUYỆN gửi prompt phù hợp."
            )
            return
        glossary_path = GLOSSARY_PROFILES[profile_name]
        if not glossary_path.is_file():
            messagebox.showerror("Thiếu từ điển", f"Không tìm thấy từ điển cho {profile_name}:\n{glossary_path}")
            return
        source = TEMP_DIR / "translation_input.txt"
        source.write_text(raw, encoding="utf-8")
        output = TRANSLATED_DIR / ((self.raw_path.stem if self.raw_path else "ban_dich") + "_vi.txt")
        self.translation_running = True
        if selected_translation_mode.startswith("ChatGPT · Qua clipboard"):
            title = self.story_title_var.get().strip() if hasattr(self, "story_title_var") else ""
            if not title and self.raw_path:
                title = self.raw_path.stem
            self._start_chatgpt_clipboard_session(
                raw, output, glossary_path, profile_name,
                source_mode=source_mode,
                novel_title=title
            )
        elif self.translation_mode_var.get().startswith("ChatGPT"):
            self.translate_note.configure(text=f"Đang chuẩn bị dịch văn học bằng ChatGPT · {profile_name}...")
            self.status.set(f"Đang dịch văn học qua ChatGPT · {profile_name}...")
            threading.Thread(
                target=self._chatgpt_translation_worker,
                args=(source, output, glossary_path),
                daemon=True
            ).start()
        else:
            self.translate_note.configure(text=f"Đang dịch bằng từ điển: {profile_name}. Lần đầu cần tải mô hình về máy.")
            self.status.set(f"Đang dịch bằng HachimiMT-60 CPU INT8 · {profile_name}...")
            threading.Thread(target=self._translation_worker, args=(source, output, glossary_path), daemon=True).start()

    def _start_chatgpt_clipboard_session(
        self,
        source_text: str,
        output: Path,
        glossary_path: Path,
        profile_name: str,
        source_mode: str = "Dịch raw Trung",
        novel_title: str = "",
    ):
        try:
            with glossary_path.open("r", encoding="utf-8-sig") as stream:
                glossary_data = json.load(stream)
            if not isinstance(glossary_data, dict):
                raise ValueError("Từ điển phải là JSON dạng Trung → Việt.")
            glossary = {
                str(key).strip(): str(value).strip()
                for key, value in glossary_data.items()
                if isinstance(key, str) and isinstance(value, str) and key.strip() and value.strip()
            }
            chapter_count, batches = split_novel_into_batches(source_text, chapters_per_batch=3)
            _chapters, chapter_labels = split_novel_into_chapters(source_text)
        except Exception as exc:
            self.translation_running = False
            messagebox.showerror("Không thể chuẩn bị dịch", str(exc))
            return
        if not batches:
            self.translation_running = False
            messagebox.showwarning("Không có nội dung", "Văn bản nguồn đang trống.")
            return

        existing = self.vi_box.get("1.0", "end-1c")
        if existing.strip():
            if not messagebox.askyesno(
                "Thay bản dịch hiện tại?",
                "Khung bản dịch hiện không trống. Bạn có muốn xóa nội dung này để bắt đầu một đợt mới?"
            ):
                self.translation_running = False
                return
            self.vi_box.delete("1.0", "end")

        session = {
            "source": source_text,
            "output": output,
            "glossary": glossary,
            "profile_name": profile_name,
            "source_mode": source_mode,
            "novel_title": novel_title.strip() or "Chưa đặt tên",
            "chapter_count": chapter_count,
            "chapter_labels": chapter_labels,
            "chapters_per_batch": 3,
            "chunks": batches,
            "translations": [],
            "index": 0,
            "phase": "research",
            "story_context": "",
            "expected_prefix": "",
            "window": None,
            "prompt_box": None,
            "prompt_heading_label": None,
            "research_label": None,
            "research_result_box": None,
            "progress_label": None,
            "controls": None,
        }
        self.clipboard_translation_session = session
        self._open_chatgpt_clipboard_window(session)
        self._manual_show_current_chunk(session)
        self._manual_update_controls(session)
        self._manual_copy_prompt()
        self._set_chatgpt_progress(
            f"Đã chuẩn bị {chapter_count} chương thành {len(batches)} lượt. Prompt nghiên cứu truyện đã sao chép."
        )

    def _open_chatgpt_clipboard_window(self, session: dict):
        window = tk.Toplevel(self.root)
        window.title("SẢNH TRUYỆN · ChatGPT · Hồ sơ truyện và dịch văn học")
        window.geometry("1000x760")
        window.minsize(800, 600)
        window.configure(bg=BG)
        window.transient(self.root)
        # Do not use grab_set: the user must still interact with the main translation pane.

        session["window"] = window
        tk.Label(
            window, text="CHATGPT · CHUẨN BỊ HỒ SƠ TRUYỆN → DỊCH TỪNG LƯỢT",
            bg=BG, fg=BRASS, font=(FONT, 13, "bold"), anchor="w"
        ).pack(fill="x", padx=18, pady=(16, 4))
        tk.Label(
            window,
            text=(
                "Lần đầu: gửi prompt nghiên cứu và dán hồ sơ ChatGPT trả về vào ô HỒ SƠ TRUYỆN. "
                "Sau đó, mỗi lượt: gửi prompt dịch, sao chép kết quả từ ChatGPT và dán vào CUỐI khung "
                "BẢN DỊCH TIẾNG VIỆT ở cửa sổ chính. Quay lại đây bấm nút xác nhận để sang lượt tiếp theo."
            ),
            bg=BG, fg=MUTED, font=(FONT, 9), anchor="w", justify="left", wraplength=950
        ).pack(fill="x", padx=18, pady=(0, 10))

        progress = tk.Label(window, text="", bg=BG, fg=INK, font=(FONT, 10, "bold"), anchor="w")
        progress.pack(fill="x", padx=18, pady=(0, 6))
        session["progress_label"] = progress

        prompt_heading = tk.Label(window, text="PROMPT CHATGPT", bg=BG, fg=BRASS,
                                  font=(FONT, 9, "bold"), anchor="w")
        prompt_heading.pack(fill="x", padx=18)
        session["prompt_heading_label"] = prompt_heading
        prompt_box = tk.Text(
            window, height=13, bg=PANEL, fg=INK, insertbackground=INK, wrap="word",
            relief="flat", font=(FONT, 9), padx=10, pady=10, undo=False
        )
        prompt_box.pack(fill="both", expand=True, padx=18, pady=(4, 8))
        prompt_box.configure(state="disabled")
        session["prompt_box"] = prompt_box

        research_label = tk.Label(
            window, text="HỒ SƠ TRUYỆN · DÁN CÂU TRẢ LỜI NGHIÊN CỨU CỦA CHATGPT VÀO ĐÂY",
            bg=BG, fg=BRASS, font=(FONT, 9, "bold"), anchor="w"
        )
        research_label.pack(fill="x", padx=18)
        session["research_label"] = research_label
        research_box = tk.Text(
            window, height=7, bg="#100C0B", fg=INK, insertbackground=INK, wrap="word",
            relief="flat", font=(FONT, 9), padx=10, pady=10, undo=True
        )
        research_box.pack(fill="both", expand=True, padx=18, pady=(4, 10))
        session["research_result_box"] = research_box

        controls = tk.Frame(window, bg=BG)
        controls.pack(fill="x", padx=18, pady=(0, 16))
        session["controls"] = controls
        window.protocol("WM_DELETE_WINDOW", self._manual_close_window)

    def _manual_update_controls(self, session: dict):
        controls = session.get("controls")
        if controls is None or not controls.winfo_exists():
            return
        for child in controls.winfo_children():
            child.destroy()
        if session.get("phase") == "research":
            self._button(controls, "MỞ CHATGPT + SAO CHÉP PROMPT NGHIÊN CỨU",
                         self._manual_copy_prompt_and_open, True)
            self._button(controls, "SAO CHÉP LẠI PROMPT",
                         self._manual_copy_prompt)
            self._button(controls, "LƯU HỒ SƠ & TẠO PROMPT LƯỢT 1",
                         self._manual_accept_answer, True)
            self._button(controls, "HỦY PHIÊN DỊCH",
                         self._manual_cancel_session)
        else:
            self._button(controls, "MỞ CHATGPT + SAO CHÉP PROMPT",
                         self._manual_copy_prompt_and_open, True)
            self._button(controls, "SAO CHÉP LẠI PROMPT",
                         self._manual_copy_prompt)
            self._button(controls, "ĐÃ DÁN KẾT QUẢ · TIẾP TỤC",
                         self._manual_accept_answer, True)
            self._button(controls, "HỦY PHIÊN DỊCH",
                         self._manual_cancel_session)

    def _manual_close_window(self):
        session = getattr(self, "clipboard_translation_session", None)
        if session and messagebox.askyesno(
            "Đóng cửa sổ dịch?",
            "Đóng cửa sổ sẽ hủy phiên clipboard hiện tại nhưng giữ nguyên nội dung đã dán trong khung bản dịch. Đóng cửa sổ?"
        ):
            self._manual_cancel_session()
        elif session:
            return

    def _manual_show_current_chunk(self, session: dict):
        progress = session.get("progress_label")
        prompt_box = session.get("prompt_box")
        prompt_heading = session.get("prompt_heading_label")
        research_label = session.get("research_label")
        research_box = session.get("research_result_box")
        phase = session.get("phase")

        if phase == "research":
            prompt = build_story_research_prompt(
                novel_title=session["novel_title"],
                source_mode=session["source_mode"],
                source_text=session["source"],
                glossary=session["glossary"],
            )
            session["current_prompt"] = prompt
            if progress is not None and progress.winfo_exists():
                progress.configure(
                    text=(
                        f"BƯỚC 1/2 · NGHIÊN CỨU TRUYỆN · {session['chapter_count']} CHƯƠNG · "
                        f"KIỂU: {session['source_mode']}"
                    )
                )
            if prompt_heading is not None and prompt_heading.winfo_exists():
                prompt_heading.configure(text="PROMPT 1 · NGHIÊN CỨU VÀ LẬP HỒ SƠ TRUYỆN")
            if research_label is not None and research_label.winfo_exists():
                research_label.configure(text="HỒ SƠ TRUYỆN · DÁN CÂU TRẢ LỜI NGHIÊN CỨU CỦA CHATGPT VÀO ĐÂY")
                if not research_label.winfo_manager():
                    research_label.pack(fill="x", padx=18)
            if research_box is not None and research_box.winfo_exists():
                if not research_box.winfo_manager():
                    research_box.pack(fill="both", expand=True, padx=18, pady=(4, 10))
                research_box.delete("1.0", "end")
        else:
            index = session["index"]
            total = len(session["chunks"])
            source_chunk = session["chunks"][index]
            first_index = index * session["chapters_per_batch"]
            last_index = min((index + 1) * session["chapters_per_batch"], session["chapter_count"]) - 1
            first_label = session["chapter_labels"][first_index]
            last_label = session["chapter_labels"][last_index]
            previous = "\n\n".join(session["translations"][-2:])
            prompt = build_clipboard_translation_prompt(
                source_chunk,
                session["glossary"],
                previous_translation=previous,
                story_context=session.get("story_context", ""),
                source_mode=session["source_mode"],
                novel_title=session["novel_title"],
                batch_number=index + 1,
                batch_total=total,
            )
            session["current_prompt"] = prompt
            session["expected_prefix"] = self.vi_box.get("1.0", "end-1c")
            # Put the insertion caret at the end so pasting a batch appends by default.
            self.vi_box.mark_set("insert", "end-1c")
            self.vi_box.see("end")
            if progress is not None and progress.winfo_exists():
                progress.configure(
                    text=(
                        f"LƯỢT {index + 1}/{total} · {session['source_mode'].upper()} · "
                        f"{first_label} → {last_label} · {len(source_chunk):,} ký tự"
                    )
                )
            if prompt_heading is not None and prompt_heading.winfo_exists():
                prompt_heading.configure(text=f"PROMPT DỊCH · LƯỢT {index + 1}/{total}")
            if research_label is not None and research_label.winfo_exists():
                research_label.pack_forget()
            if research_box is not None and research_box.winfo_exists():
                research_box.pack_forget()

        if prompt_box is not None and prompt_box.winfo_exists():
            prompt_box.configure(state="normal")
            prompt_box.delete("1.0", "end")
            prompt_box.insert("1.0", session["current_prompt"])
            prompt_box.configure(state="disabled")

    def _manual_copy_prompt(self):
        session = getattr(self, "clipboard_translation_session", None)
        if not session:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(session["current_prompt"])
            self.root.update()
            if session.get("phase") == "research":
                detail = "Đã sao chép prompt nghiên cứu truyện. Gửi prompt này trong ChatGPT trước khi dịch lượt 1."
            else:
                detail = (
                    f"Đã sao chép prompt lượt {session['index'] + 1}/{len(session['chunks'])}. "
                    "Dán và gửi trong cùng cuộc trò chuyện ChatGPT."
                )
            self._set_chatgpt_progress(detail)
        except tk.TclError as exc:
            messagebox.showerror("Không sao chép được", f"Clipboard không khả dụng: {exc}")

    def _manual_copy_prompt_and_open(self):
        self._manual_copy_prompt()
        try:
            webbrowser.open("https://chatgpt.com/")
        except Exception as exc:
            messagebox.showwarning("Không mở được ChatGPT", f"Hãy tự mở https://chatgpt.com/ trong trình duyệt.\n{exc}")

    def _manual_accept_answer(self):
        session = getattr(self, "clipboard_translation_session", None)
        if not session:
            return
        if session.get("phase") == "research":
            self._manual_accept_research_profile(session)
            return

        current_text = self.vi_box.get("1.0", "end-1c")
        expected_prefix = session.get("expected_prefix", "")
        if not current_text.startswith(expected_prefix):
            messagebox.showwarning(
                "Bản dịch cũ đã bị thay đổi",
                "SẢNH TRUYỆN phát hiện phần văn bản đã chấp nhận trước đó bị sửa hoặc xóa. "
                "Hãy khôi phục phần trước, đặt con trỏ ở cuối khung BẢN DỊCH TIẾNG VIỆT rồi dán câu trả lời mới."
            )
            return
        new_answer = current_text[len(expected_prefix):].strip("\r\n")
        if not new_answer.strip():
            messagebox.showwarning(
                "Chưa dán kết quả",
                "Hãy sao chép câu trả lời trong ChatGPT, dán vào CUỐI khung BẢN DỊCH TIẾNG VIỆT bên phải, rồi bấm nút này."
            )
            return
        index = session["index"]
        total = len(session["chunks"])
        if not messagebox.askyesno(
            "Xác nhận bản dịch",
            f"Bạn đã dán kết quả cho lượt {index + 1}/{total} vào khung bản dịch chưa?\n\n"
            "Sau khi xác nhận, SẢNH TRUYỆN sẽ giữ nguyên đoạn này và chuẩn bị prompt lượt tiếp theo."
        ):
            return

        prefix = expected_prefix.rstrip("\r\n")
        assembled = (prefix + "\n\n" if prefix else "") + new_answer
        self.vi_box.delete("1.0", "end")
        self.vi_box.insert("1.0", assembled)
        self.vi_box.mark_set("insert", "end-1c")
        self.vi_box.see("end")
        session["translations"].append(new_answer)
        session["index"] += 1

        if session["index"] >= total:
            window = session.get("window")
            self.clipboard_translation_session = None
            self.translation_running = False
            self.translate_note.configure(
                text=f"Đã gom đủ {session['chapter_count']} chương trong {total} lượt · hãy bấm LƯU BẢN DỊCH."
            )
            self.status.set("Đã dịch xong qua ChatGPT clipboard. Hãy bấm LƯU BẢN DỊCH để lưu TXT.")
            if window is not None and window.winfo_exists():
                window.destroy()
            messagebox.showinfo(
                "Đã hoàn tất đợt dịch",
                f"Đã gom đủ {session['chapter_count']} chương trong {total} lượt.\n\n"
                "Bản dịch đang nằm trong khung BẢN DỊCH TIẾNG VIỆT. "
                "Hãy kiểm tra rồi bấm LƯU BẢN DỊCH để lưu TXT về máy."
            )
            return

        self._manual_show_current_chunk(session)
        self._manual_update_controls(session)
        self._manual_copy_prompt()
        first_label = session["chapter_labels"][session["index"] * session["chapters_per_batch"]]
        last_index = min(
            (session["index"] + 1) * session["chapters_per_batch"],
            session["chapter_count"]
        ) - 1
        last_label = session["chapter_labels"][last_index]
        messagebox.showinfo(
            "Đã nhận kết quả · chuẩn bị lượt tiếp theo",
            f"Đã nhận lượt {index + 1}/{total}. Prompt lượt {session['index'] + 1}/{total} "
            f"({first_label} → {last_label}) đã được sao chép.\n\n"
            "Quay lại cùng cuộc trò chuyện ChatGPT, dán và gửi prompt mới. Khi ChatGPT trả lời xong, "
            "sao chép bản dịch, dán vào CUỐI khung BẢN DỊCH TIẾNG VIỆT bên phải rồi quay lại bấm ĐÃ DÁN KẾT QUẢ."
        )

    def _manual_accept_research_profile(self, session: dict):
        box = session.get("research_result_box")
        if box is None or not box.winfo_exists():
            return
        story_context = box.get("1.0", "end-1c").strip()
        if not story_context:
            messagebox.showwarning(
                "Chưa có hồ sơ truyện",
                "Hãy gửi prompt nghiên cứu trong ChatGPT, sao chép câu trả lời về hồ sơ nhân vật/thuật ngữ rồi dán vào ô HỒ SƠ TRUYỆN."
            )
            return
        if not messagebox.askyesno(
            "Lưu hồ sơ truyện?",
            "Dùng hồ sơ này làm dữ liệu tham chiếu cho cả 5 lượt dịch trong đợt hiện tại?"
        ):
            return
        session["story_context"] = story_context
        session["phase"] = "translation"
        session["index"] = 0
        self._manual_show_current_chunk(session)
        self._manual_update_controls(session)
        self._manual_copy_prompt()
        messagebox.showinfo(
            "Đã lưu hồ sơ · prompt lượt 1 sẵn sàng",
            "Hồ sơ truyện đã được lưu trong phiên hiện tại. Prompt dịch lượt 1 đã được sao chép.\n\n"
            "Quay lại CÙNG cuộc trò chuyện ChatGPT đã dùng để nghiên cứu, dán prompt lượt 1 và gửi. "
            "Khi dịch xong, sao chép phần dịch và dán vào CUỐI khung BẢN DỊCH TIẾNG VIỆT ở cửa sổ SẢNH TRUYỆN chính."
        )

    def _manual_cancel_session(self):
        session = getattr(self, "clipboard_translation_session", None)
        if session:
            window = session.get("window")
            self.clipboard_translation_session = None
            self.translation_running = False
            self.translate_note.configure(
                text="Đã hủy phiên dịch qua clipboard; nội dung đã dán vẫn được giữ trong khung bản dịch."
            )
            self.status.set("Đã hủy phiên dịch qua clipboard.")
            if window is not None and window.winfo_exists():
                window.destroy()

    def _start_chatgpt_account_command(self, command: str):
        if self.translation_running:
            messagebox.showwarning("Đang xử lý", "Hãy đợi tác vụ hiện tại hoàn tất trước khi thay đổi kết nối ChatGPT.")
            return
        if not TRANSLATION_PYTHON.is_file():
            messagebox.showerror(
                "Chưa cài bộ dịch",
                f"Không tìm thấy môi trường Python:\n{TRANSLATION_PYTHON}\n\n"
                "Hãy chạy scripts\\setup_translation.ps1 để cài thư viện tích hợp ChatGPT."
            )
            return
        if not CHATGPT_TRANSLATION_SCRIPT.is_file():
            messagebox.showerror("Thiếu mô-đun ChatGPT", f"Không tìm thấy file:\n{CHATGPT_TRANSLATION_SCRIPT}")
            return
        action_text = "Đang mở đăng nhập ChatGPT..." if command == "login" else "Đang ngắt kết nối ChatGPT..."
        self.translation_running = True
        self.translate_note.configure(text=action_text)
        self.status.set(action_text)
        threading.Thread(
            target=self._chatgpt_account_command_worker, args=(command,), daemon=True
        ).start()

    def start_chatgpt_login(self):
        if not messagebox.askyesno(
            "Kết nối ChatGPT",
            "SẢNH TRUYỆN sẽ mở trang OpenAI để bạn đăng nhập và xem các quyền được yêu cầu. "
            "Văn bản truyện chỉ được gửi đi khi bạn chọn chế độ ChatGPT và bấm BẮT ĐẦU DỊCH.\n\n"
            "Tính năng dùng hạn mức ChatGPT hiện chỉ dành cho tài khoản/workspace đủ điều kiện (thường là Plus/Pro). "
            "Tài khoản Free có thể đăng nhập nhưng không được bảo đảm quyền dịch.\n\nTiếp tục?"
        ):
            return
        self._start_chatgpt_account_command("login")

    def start_chatgpt_logout(self):
        if not messagebox.askyesno(
            "Ngắt kết nối ChatGPT",
            "SẢNH TRUYỆN sẽ thử thu hồi phiên đăng nhập rồi xóa thông tin xác thực đã lưu trên máy này. Tiếp tục?"
        ):
            return
        self._start_chatgpt_account_command("logout")

    def _set_chatgpt_progress(self, text: str):
        try:
            note = getattr(self, "translate_note", None)
            if note is not None and note.winfo_exists():
                note.configure(text=text)
            self.status.set(text)
        except tk.TclError:
            pass

    def _chatgpt_account_command_worker(self, command: str):
        lines = []
        try:
            proc = subprocess.Popen(
                [str(TRANSLATION_PYTHON), "-X", "utf8", str(CHATGPT_TRANSLATION_SCRIPT), command],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                cwd=str(ROOT_DIR)
            )
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line = raw_line.rstrip()
                if not line:
                    continue
                lines.append(line)
                if line.startswith("[STATUS]"):
                    self.root.after(0, lambda detail=line: self._set_chatgpt_progress(detail[8:].strip()))
            return_code = proc.wait()
            output = "\n".join(lines[-30:])
            self.root.after(0, lambda: self._chatgpt_account_command_finished(command, return_code, output))
        except Exception as exc:
            self.root.after(0, lambda detail=str(exc): self._chatgpt_account_command_finished(command, 1, detail))

    def _chatgpt_account_command_finished(self, command: str, return_code: int, detail: str):
        self.translation_running = False
        if return_code == 0:
            self.status.set("Đã cập nhật kết nối ChatGPT.")
            if command == "logout":
                messagebox.showinfo("Đã ngắt kết nối", detail or "Đã xóa thông tin đăng nhập ChatGPT khỏi máy này.")
            elif "[WARNING]" in detail:
                messagebox.showwarning(
                    "Đã đăng nhập nhưng chưa có quyền dịch",
                    detail + "\n\nBạn có thể dùng chế độ HachimiMT-60. Muốn dịch qua hạn mức ChatGPT, tài khoản/workspace cần được OpenAI cấp quyền."
                )
            else:
                messagebox.showinfo("Kết nối thành công", detail or "Đã kết nối ChatGPT. Chọn “ChatGPT · Dịch văn học” để dùng.")
            self._set_chatgpt_progress("Đã hoàn tất thao tác ChatGPT.")
        else:
            self.status.set("Thao tác ChatGPT thất bại.")
            self._set_chatgpt_progress("Thao tác ChatGPT thất bại.")
            messagebox.showerror("Không hoàn tất được ChatGPT", detail or "Không có thông tin lỗi.")

    def _chatgpt_translation_worker(self, source: Path, output: Path, glossary_path: Path):
        lines = []
        try:
            proc = subprocess.Popen(
                [
                    str(TRANSLATION_PYTHON), "-X", "utf8", str(CHATGPT_TRANSLATION_SCRIPT),
                    "translate", "--input", str(source), "--output", str(output),
                    "--glossary", str(glossary_path)
                ],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                cwd=str(ROOT_DIR)
            )
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line = raw_line.rstrip()
                if not line:
                    continue
                lines.append(line)
                if line.startswith("[STATUS]"):
                    self.root.after(0, lambda detail=line: self._set_chatgpt_progress(detail[8:].strip()))
                elif line.startswith("[PROGRESS]"):
                    self.root.after(0, lambda detail=line: self._set_chatgpt_progress("Đang dịch văn học · " + detail[10:].strip()))
            return_code = proc.wait()
            if return_code != 0:
                raise RuntimeError("\n".join(lines[-30:]) or "Dịch qua ChatGPT thất bại.")
            translated = output.read_text(encoding="utf-8")
            self.root.after(0, lambda: self._translation_succeeded(output, translated))
        except Exception as exc:
            self.root.after(0, lambda detail=str(exc): self._translation_failed(detail))

    def _translation_worker(self, source: Path, output: Path, glossary_path: Path):
        try:
            # Force UTF-8 in the child interpreter too: Windows legacy code pages can
            # otherwise fail when libraries print Vietnamese status/error messages.
            translation_env = os.environ.copy()
            translation_env["PYTHONUTF8"] = "1"
            translation_env["PYTHONIOENCODING"] = "utf-8:replace"
            translation_env.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
            proc = subprocess.run(
                [str(TRANSLATION_PYTHON), "-X", "utf8", str(TRANSLATION_SCRIPT), "--input", str(source),
                 "--output", str(output), "--cache-dir", str(MODEL_CACHE_DIR),
                 "--glossary", str(glossary_path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=None,
                cwd=str(ROOT_DIR), env=translation_env,
            )
            if proc.returncode != 0:
                raise RuntimeError((proc.stderr or proc.stdout or "Bộ dịch trả về lỗi").strip())
            translated = output.read_text(encoding="utf-8")
            self.root.after(0, lambda: self._translation_succeeded(output, translated))
        except Exception as exc:
            self.root.after(0, lambda detail=str(exc): self._translation_failed(detail))

    def _translation_succeeded(self, output: Path, translated: str):
        self.translation_running = False
        self.vi_box.delete("1.0", "end"); self.vi_box.insert("1.0", translated)
        self.translate_note.configure(text=f"Hoàn tất: {output.name}")
        self.status.set(f"Đã dịch xong: {output.name}")
        messagebox.showinfo("Dịch hoàn tất", f"Đã lưu bản dịch tại:\n{output}")

    def _translation_failed(self, detail: str):
        self.translation_running = False
        self.translate_note.configure(text="Dịch thất bại · xem thông báo lỗi")
        self.status.set("Dịch thất bại.")
        messagebox.showerror("Không dịch được", detail)

    def show_audio(self):
        page = self._clear("audio")
        self._heading(
            page, "03 / Listen", "Phòng thu audio",
            "Biến bản truyện thành MP3 dài với giọng đọc và tốc độ bạn lựa chọn."
        )

        workspace = tk.Frame(page, bg=BG)
        workspace.pack(fill="both", expand=True, padx=26, pady=(0, 14))
        workspace.grid_rowconfigure(0, weight=1)
        workspace.grid_columnconfigure(0, weight=3, uniform="audio_columns")
        workspace.grid_columnconfigure(1, weight=2, uniform="audio_columns")

        left = self._panel(
            workspace, "NỘI DUNG AUDIO",
            "Dán toàn bộ nội dung hoặc mở một file TXT đã dịch."
        )
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 7), pady=3)

        right = self._panel(
            workspace, "KẾT QUẢ AUDIO",
            "Theo dõi tiến trình và mở các file MP3 đã tạo."
        )
        right.grid(row=0, column=1, sticky="nsew", padx=(7, 0), pady=3)

        load_bar = tk.Frame(left, bg=PANEL)
        load_bar.pack(fill="x", padx=14, pady=(2, 5))
        self._button(load_bar, "MỞ FILE TXT", self.select_audio_file, True, width=140)
        self.audio_file_label = tk.Label(
            load_bar, text="Chưa nạp file · có thể dán văn bản trực tiếp",
            bg=PANEL, fg=MUTED, font=(FONT, 8), anchor="w",
            wraplength=300, justify="left"
        )
        self.audio_file_label.pack(side="left", fill="x", expand=True, padx=7)

        create_row = tk.Frame(left, bg=PANEL)
        create_row.pack(side="bottom", fill="x", padx=14, pady=(5, 12))
        self._button(
            create_row, "TẠO AUDIO", self.start_audio_generation_github,
            True, width=190
        )
        tk.Label(
            create_row, text="Xuất một file MP3 hoàn chỉnh",
            bg=PANEL, fg=MUTED, font=(FONT, 8)
        ).pack(side="left", padx=5)

        options = tk.Frame(left, bg=PANEL2,
                           highlightbackground="#30201B", highlightthickness=1)
        options.pack(side="bottom", fill="x", padx=14, pady=(8, 2))
        tk.Label(options, text="GIỌNG ĐỌC", bg=PANEL2, fg=BRASS,
                 font=(FONT, 8, "bold")).grid(row=0, column=0, sticky="w", padx=(10, 6), pady=8)
        if not hasattr(self, "voice_var"):
            self.voice_var = tk.StringVar(value="Nam Minh")
        ttk.Combobox(
            options, textvariable=self.voice_var, values=("Nam Minh", "Hoài My"),
            state="readonly", width=12, style="Dark.TCombobox"
        ).grid(row=0, column=1, sticky="ew", padx=(0, 14), pady=5)
        tk.Label(options, text="TỐC ĐỘ", bg=PANEL2, fg=BRASS,
                 font=(FONT, 8, "bold")).grid(row=0, column=2, sticky="w", padx=(0, 6), pady=8)
        if not hasattr(self, "rate_var"):
            self.rate_var = tk.StringVar(value="1.0")
        ttk.Combobox(
            options, textvariable=self.rate_var,
            values=("0.75", "1.0", "1.25", "1.5", "1.75", "2.0"),
            state="readonly", width=7, style="Dark.TCombobox"
        ).grid(row=0, column=3, sticky="ew", padx=(0, 10), pady=5)
        options.grid_columnconfigure(1, weight=1)
        options.grid_columnconfigure(3, weight=1)

        editor = tk.Frame(left, bg="#100C0B", highlightbackground=BORDER, highlightthickness=1)
        editor.pack(fill="both", expand=True, padx=14, pady=(6, 4))
        editor.grid_rowconfigure(0, weight=1)
        editor.grid_columnconfigure(0, weight=1)
        audio_scroll = tk.Scrollbar(editor, orient="vertical", bg=PANEL2,
                                    troughcolor="#090707", activebackground=RED)
        self.audio_text = tk.Text(
            editor, bg="#100C0B", fg=INK, insertbackground=INK, wrap="word",
            relief="flat", font=(FONT, 10), padx=12, pady=12, undo=True,
            yscrollcommand=audio_scroll.set, highlightthickness=0, bd=0
        )
        audio_scroll.configure(command=self.audio_text.yview)
        self.audio_text.grid(row=0, column=0, sticky="nsew")
        audio_scroll.grid(row=0, column=1, sticky="ns")
        self.audio_text.insert("1.0", getattr(self, "audio_draft", ""))
        self.audio_text.edit_modified(False)
        self.audio_text.bind("<<Modified>>", self._remember_audio_draft)

        result_bottom = tk.Frame(right, bg=PANEL)
        result_bottom.pack(side="bottom", fill="x", padx=14, pady=(4, 12))
        self.audio_note = tk.Label(
            result_bottom, text="Sẵn sàng · chưa có tác vụ mới",
            bg=PANEL, fg=MUTED, font=(FONT, 9), anchor="w",
            wraplength=310, justify="left"
        )
        self.audio_note.pack(fill="x", pady=(0, 6))
        result_buttons = tk.Frame(result_bottom, bg=PANEL)
        result_buttons.pack(fill="x")
        self._button(result_buttons, "NGHE / MỞ FILE", self.open_selected_audio_output, True, width=145)
        self._button(result_buttons, "MỞ THƯ MỤC", lambda: self.open_folder(AUDIO_DIR), width=115)

        self.audio_status_card = tk.Frame(
            right, bg=PANEL2, highlightbackground=BORDER, highlightthickness=1
        )
        self.audio_status_card.pack(side="bottom", fill="x", padx=14, pady=(4, 3))
        self.audio_status_icon = tk.Canvas(
            self.audio_status_card, width=34, height=34, bg=PANEL2,
            highlightthickness=0, bd=0
        )
        self.audio_status_icon.pack(side="left", padx=(10, 7), pady=7)
        status_text = tk.Frame(self.audio_status_card, bg=PANEL2)
        status_text.pack(side="left", fill="both", expand=True, pady=7)
        self.audio_status_title = tk.Label(
            status_text, text="Chưa tạo audio", bg=PANEL2, fg=MUTED,
            font=(FONT, 10, "bold"), anchor="w"
        )
        self.audio_status_title.pack(fill="x")
        self.audio_status_detail = tk.Label(
            status_text, text="Nhấn TẠO AUDIO để bắt đầu", bg=PANEL2, fg=MUTED,
            font=(FONT, 8), anchor="w", wraplength=220, justify="left"
        )
        self.audio_status_detail.pack(fill="x", pady=(2, 0))
        self.audio_status_timer = tk.Label(
            self.audio_status_card, text="—", bg=PANEL2, fg=INK,
            font=(FONT, 11, "bold"), anchor="e"
        )
        self.audio_status_timer.pack(side="right", padx=(6, 12))

        tk.Label(right, text="LỊCH SỬ AUDIO", bg=PANEL, fg=BRASS,
                 font=(FONT, 9, "bold")).pack(anchor="w", padx=14, pady=(8, 0))
        self.audio_list = tk.Listbox(
            right, bg="#0E0A09", fg=INK, selectbackground="#641A23",
            relief="flat", height=10, font=(FONT, 9), activestyle="none",
            selectmode="browse", highlightthickness=1,
            highlightbackground="#30201B", highlightcolor=RED
        )
        self.audio_list.pack(fill="both", expand=True, padx=14, pady=(5, 4))
        self.audio_list_paths = {}
        self._render_audio_history()
        self._render_audio_status()

    def _remember_audio_draft(self, _event=None):
        try:
            if self.audio_text.edit_modified():
                self.audio_draft = self.audio_text.get("1.0", "end-1c")
                self.audio_text.edit_modified(False)
        except tk.TclError:
            pass

    def select_audio_file(self):
        path = filedialog.askopenfilename(
            title="Chọn một file TXT dài để tạo thành một audio",
            initialdir=str(TRANSLATED_DIR),
            filetypes=[("Text", "*.txt"), ("All files", "*.*")]
        )
        if not path:
            return
        try:
            content = Path(path).read_text(encoding="utf-8-sig")
        except Exception as exc:
            messagebox.showerror("Không đọc được TXT", str(exc))
            return
        self.audio_path = Path(path)
        self.audio_draft = content
        self.audio_text.delete("1.0", "end")
        self.audio_text.insert("1.0", content)
        self.audio_file_label.configure(text=self.audio_path.name, fg=INK)
        self.status.set(f"Đã nạp nội dung audio: {self.audio_path.name}")

    def _render_audio_history(self):
        if not hasattr(self, "audio_list"):
            return
        try:
            if not self.audio_list.winfo_exists():
                return
        except tk.TclError:
            return
        self.audio_list.delete(0, "end")
        self.audio_list_paths = {}
        for job in self.audio_history:
            state = job.get("state", "waiting")
            if state == "generating":
                engine_label = "GITHUB ACTIONS" if job.get("engine") == "github" else "MÁY"
                label = f"⏳ GENERATE · {engine_label} · {job['stem']}"
            elif state == "completed":
                path = job.get("path")
                label = f"✓ ĐÃ XONG · {Path(path).name if path else job['stem']}"
            else:
                detail = str(job.get("error") or "")
                label = f"✗ LỖI · {job['stem']}" + (f" · {detail[:70]}" if detail else "")
            self.audio_list.insert("end", label)
            index = self.audio_list.size() - 1
            if state == "completed" and job.get("path"):
                self.audio_list_paths[index] = Path(job["path"])
        if self.audio_history:
            self.audio_list.selection_clear(0, "end")
            self.audio_list.selection_set(self.audio_list.size() - 1)
            self.audio_list.see(self.audio_list.size() - 1)

    @staticmethod
    def _format_audio_elapsed(seconds: float) -> str:
        total = max(0, int(seconds))
        if total < 60:
            return f"{total}s"
        if total < 3600:
            return f"{total // 60}m {total % 60}s"
        return f"{total // 3600}h {(total % 3600) // 60}m {total % 60}s"

    def _draw_audio_status_icon(self, state: str):
        if not hasattr(self, "audio_status_icon"):
            return
        try:
            icon = self.audio_status_icon
            if not icon.winfo_exists():
                return
            icon.delete("all")
            if state == "generating":
                icon.create_arc(
                    5, 5, 29, 29, start=self._audio_spinner_angle, extent=285,
                    style="arc", outline=BRASS, width=3
                )
                self._audio_spinner_angle = (self._audio_spinner_angle + 28) % 360
            elif state == "completed":
                icon.create_line(5, 17, 12, 24, 25, 9, fill=GREEN, width=3,
                                 capstyle="round", joinstyle="round")
            elif state == "failed":
                icon.create_line(8, 8, 26, 26, fill=RED_HOVER, width=3, capstyle="round")
                icon.create_line(26, 8, 8, 26, fill=RED_HOVER, width=3, capstyle="round")
            else:
                icon.create_oval(9, 9, 25, 25, outline=BRASS, width=2)
        except tk.TclError:
            pass

    def _render_audio_status(self):
        try:
            if self.current_page != "audio" or not self.audio_status_card.winfo_exists():
                return
        except (AttributeError, tk.TclError):
            return

        job = self.audio_history[-1] if self.audio_history else None
        if job is None:
            state = "idle"
            title = "Chưa tạo audio"
            detail = "Nhấn “TẠO MỘT FILE AUDIO” để bắt đầu"
            elapsed_text = "—"
            title_color = MUTED
        else:
            state = job.get("state", "idle")
            started_at = float(job.get("started_at") or time.monotonic())
            if state == "generating":
                elapsed = time.monotonic() - started_at
                title = "generate"
                detail = str(job.get("progress") or f"Đang tạo: {job.get('stem') or 'audio'}")
                title_color = BRASS
            elif state == "completed":
                elapsed = float(job.get("duration_seconds") or 0)
                title = "completed"
                detail = f"Đã tạo xong: {Path(job.get('path') or job.get('stem') or 'audio').name}"
                title_color = GREEN
            else:
                elapsed = float(job.get("duration_seconds") or 0)
                title = "failed"
                detail = f"Tạo thất bại: {job.get('stem') or 'audio'}"
                title_color = RED_HOVER
            elapsed_text = self._format_audio_elapsed(elapsed)

        self.audio_status_title.configure(text=title, fg=title_color)
        self.audio_status_detail.configure(text=detail)
        self.audio_status_timer.configure(text=elapsed_text)
        self._draw_audio_status_icon(state)

        # A single timer drives both the live clock and the spinner animation.
        if state == "generating":
            self._schedule_audio_status_tick()

    def _schedule_audio_status_tick(self):
        if self._audio_status_after_id is not None or self.current_page != "audio":
            return
        try:
            if not self.audio_status_card.winfo_exists():
                return
            self._audio_status_after_id = self.root.after(120, self._audio_status_tick)
        except (AttributeError, tk.TclError):
            self._audio_status_after_id = None

    def _audio_status_tick(self):
        self._audio_status_after_id = None
        self._render_audio_status()

    def _finish_audio_clock(self, job: dict):
        finished_at = time.monotonic()
        started_at = float(job.get("started_at") or finished_at)
        job["finished_at"] = finished_at
        job["duration_seconds"] = max(0.0, finished_at - started_at)

    def start_audio_generation(self):
        if self.audio_running:
            messagebox.showinfo(
                "Đang tạo audio",
                "Một audio đang được tạo. Hãy chờ hoàn tất rồi tạo lượt tiếp theo để tránh gửi nhiều yêu cầu cùng lúc."
            )
            return

        text = self.audio_text.get("1.0", "end-1c").strip()
        if not text:
            messagebox.showwarning("Chưa có nội dung", "Hãy dán toàn bộ nội dung hoặc mở một file TXT trước.")
            return
        if not AUDIO_PYTHON.is_file():
            messagebox.showerror(
                "Chưa cài bộ audio",
                f"Không tìm thấy môi trường audio:\n{AUDIO_PYTHON}\n\nChạy scripts\\setup_audio.ps1 trước."
            )
            return
        if not AUDIO_TTS_GENERATOR.is_file():
            messagebox.showerror(
                "Chưa cập nhật bộ tạo audio dài",
                f"Không tìm thấy bộ tạo audio dài:\n{AUDIO_TTS_GENERATOR}\n\n"
                "Chạy scripts\\update_long_audio_tts.ps1 trước."
            )
            return

        stem = self.audio_path.stem if self.audio_path else "audio_dai"
        job_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        job = {
            "id": job_id,
            "text": text,
            "stem": stem,
            "voice": self.voice_var.get(),
            "rate": self.rate_var.get(),
            "state": "generating",
            "engine": "local",
            "path": None,
            "error": None,
            "started_at": time.monotonic(),
            "finished_at": None,
            "duration_seconds": None,
        }
        self.audio_history.append(job)
        self.audio_running = True
        self.audio_draft = text
        self.audio_note.configure(text="Đang gửi toàn bộ nội dung để tạo một file MP3 duy nhất...")
        self.status.set("Đang tạo một audio dài · vui lòng giữ ứng dụng mở.")
        self._render_audio_history()
        self._render_audio_status()
        threading.Thread(target=self._audio_worker, args=(job,), daemon=True).start()

    def start_audio_generation_github(self):
        if self.audio_running:
            messagebox.showinfo(
                "Đang tạo audio",
                "Một audio đang được tạo. Hãy chờ hoàn tất rồi tạo lượt tiếp theo."
            )
            return
        text = self.audio_text.get("1.0", "end-1c").strip()
        if not text:
            messagebox.showwarning("Chưa có nội dung", "Hãy dán nội dung hoặc mở một file TXT trước.")
            return
        if not shutil.which("gh"):
            messagebox.showerror(
                "Chưa cài GitHub CLI",
                "Để tạo audio bằng GitHub Actions, cần cài GitHub CLI rồi đăng nhập một lần.\n\n"
                "1. Mở PowerShell và chạy:\n"
                "winget install --id GitHub.cli -e\n\n"
                "2. Đóng, mở lại PowerShell rồi chạy:\n"
                "gh auth login\n\n"
                "Làm theo hướng dẫn trên màn hình và đăng nhập tài khoản GitHub của bạn."
            )
            return

        stem = self.audio_path.stem if self.audio_path else "audio_dai"
        job_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        request_id = "st-" + uuid.uuid4().hex[:16]
        job = {
            "id": job_id,
            "text": text,
            "stem": stem,
            "voice": self.voice_var.get(),
            "rate": self.rate_var.get(),
            "state": "generating",
            "engine": "github",
            "path": None,
            "error": None,
            "progress": "Đang kiểm tra đăng nhập và quyền truy cập GitHub...",
            "request_id": request_id,
            "started_at": time.monotonic(),
            "finished_at": None,
            "duration_seconds": None,
        }
        self.audio_history.append(job)
        self.audio_running = True
        self.audio_draft = text
        self.audio_note.configure(text="Đang chuẩn bị gửi chương lên GitHub Actions...")
        self.status.set("Đang tạo audio bằng GitHub Actions · vui lòng giữ ứng dụng mở.")
        self._render_audio_history()
        self._render_audio_status()
        threading.Thread(target=self._github_audio_worker, args=(job,), daemon=True).start()

    def _set_audio_job_progress(self, job_id: str, message: str):
        job = next((item for item in self.audio_history if item["id"] == job_id), None)
        if job is None:
            return
        job["progress"] = message
        if self.current_page == "audio":
            try:
                if self.audio_note.winfo_exists():
                    self.audio_note.configure(text=message)
            except tk.TclError:
                pass
            self._render_audio_status()

    def _run_github_cli(self, args: list[str], input_text: str | None = None,
                        timeout: int = 60) -> subprocess.CompletedProcess:
        executable = shutil.which("gh")
        if not executable:
            raise RuntimeError(
                "Không tìm thấy GitHub CLI (gh). Hãy cài bằng "
                "'winget install --id GitHub.cli -e', sau đó chạy 'gh auth login'."
            )
        try:
            return subprocess.run(
                [executable, *args],
                input=input_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=str(ROOT_DIR),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("GitHub CLI hết thời gian chờ khi gọi API.") from exc

    def _github_audio_worker(self, job: dict):
        job_id = job["id"]
        request_id = job["request_id"]
        repo = GITHUB_AUDIO_REPO
        ref = GITHUB_AUDIO_REF
        download_dir = TEMP_DIR / f"github_audio_{job_id}"
        try:
            if repo.count("/") != 1:
                raise RuntimeError(
                    "Tên repository GitHub chưa hợp lệ. Hãy dùng dạng owner/repository."
                )

            auth = self._run_github_cli(["auth", "status", "--hostname", "github.com"])
            if auth.returncode != 0:
                detail = (auth.stderr or auth.stdout or "").strip()
                raise RuntimeError(
                    "Chưa đăng nhập GitHub CLI. Mở PowerShell, chạy 'gh auth login' "
                    "và hoàn tất hướng dẫn đăng nhập.\n" + detail[:1200]
                )

            repo_result = self._run_github_cli(["api", f"repos/{repo}"])
            if repo_result.returncode != 0:
                raise RuntimeError(
                    "Không đọc được repository GitHub. Hãy kiểm tra tài khoản đã đăng nhập "
                    "có quyền truy cập repository này.\n"
                    + (repo_result.stderr or repo_result.stdout or "")[-1600:]
                )
            try:
                repo_data = json.loads(repo_result.stdout)
            except json.JSONDecodeError as exc:
                raise RuntimeError("GitHub trả về thông tin repository không hợp lệ.") from exc
            if repo_data.get("private") is not True:
                raise RuntimeError(
                    f"Repository {repo} hiện đang Public. Vì nội dung chương được gửi lên "
                    "GitHub Actions, SẢNH TRUYỆN sẽ không gửi văn bản truyện tới repository công khai. "
                    "Hãy chuyển repository này sang Private trong GitHub Settings, hoặc cấu hình "
                    "một repository Private có workflow Generate long audio."
                )

            workflow_path = f"repos/{repo}/actions/workflows/{GITHUB_AUDIO_WORKFLOW}"
            workflow_info = self._run_github_cli(["api", workflow_path])
            if workflow_info.returncode != 0:
                raise RuntimeError(
                    "Không tìm thấy workflow Generate long audio trong repository.\n"
                    + (workflow_info.stderr or workflow_info.stdout or "")[-1600:]
                )

            upload_dir = TEMP_DIR / f"github_audio_upload_{job_id}"
            upload_dir.mkdir(parents=True, exist_ok=True)
            upload_path = upload_dir / "story.txt"
            upload_path.write_text(job["text"], encoding="utf-8")
            tag = f"st-request-{request_id}"
            dispatched = False

            upload = self._run_github_cli(
                [
                    "release", "create", tag, str(upload_path),
                    "--repo", repo,
                    "--title", f"SẢNH TRUYỆN upload {request_id}",
                    "--notes", "Temporary private story upload for one SẢNH TRUYỆN job.",
                ],
                timeout=180,
            )
            if upload.returncode != 0:
                raise RuntimeError(
                    "Không tải được chương lên release tạm của repository Private. "
                    "Kiểm tra quyền ghi Releases/Contents của tài khoản GitHub.\\n"
                    + (upload.stderr or upload.stdout or "")[-1800:]
                )

            runs_endpoint = (
                f"repos/{repo}/actions/workflows/{GITHUB_AUDIO_WORKFLOW}/runs"
                f"?event=workflow_dispatch&branch={ref}&per_page=50"
            )
            before_response = self._run_github_cli(["api", runs_endpoint])
            if before_response.returncode != 0:
                raise RuntimeError(
                    "Không đọc được danh sách GitHub Actions.\n"
                    + (before_response.stderr or before_response.stdout or "")[-1200:]
                )
            try:
                before_runs = json.loads(before_response.stdout).get("workflow_runs", [])
                before_ids = {int(item["id"]) for item in before_runs}
            except (json.JSONDecodeError, TypeError, KeyError, ValueError) as exc:
                raise RuntimeError("Danh sách GitHub Actions trả về không hợp lệ.") from exc

            payload = {
                "ref": ref,
                "inputs": {
                    "request_id": request_id,
                    "voice": job["voice"],
                    "rate": str(job["rate"]),
                },
            }
            dispatch = self._run_github_cli(
                [
                    "api", "--method", "POST",
                    f"{workflow_path}/dispatches",
                    "--input", "-",
                ],
                input_text=json.dumps(payload, ensure_ascii=False),
                timeout=90,
            )
            if dispatch.returncode != 0:
                raise RuntimeError(
                    "Không gửi được tác vụ tới GitHub Actions. Kiểm tra quyền Actions "
                    "và quyền ghi của tài khoản GitHub.\n"
                    + (dispatch.stderr or dispatch.stdout or "")[-2000:]
                )

            dispatched = True
            self.root.after(
                0, lambda j=job_id, m="Đã gửi chương lên GitHub · đang chờ runner...":
                self._set_audio_job_progress(j, m)
            )

            expected_title = f"SẢNH TRUYỆN {request_id}"
            discovery_deadline = time.monotonic() + 180
            remote_run = None
            while time.monotonic() < discovery_deadline:
                response = self._run_github_cli(["api", runs_endpoint], timeout=45)
                if response.returncode == 0:
                    try:
                        candidates = json.loads(response.stdout).get("workflow_runs", [])
                        new_candidates = [
                            item for item in candidates
                            if int(item.get("id", 0)) not in before_ids
                            and item.get("event") == "workflow_dispatch"
                        ]
                        remote_run = next(
                            (
                                item for item in new_candidates
                                if item.get("display_title") == expected_title
                            ),
                            None,
                        )
                        # Some GitHub API views may show the workflow name rather than
                        # the custom run-name. If exactly one new run exists, use it.
                        if remote_run is None and len(new_candidates) == 1:
                            remote_run = new_candidates[0]
                    except (json.JSONDecodeError, TypeError, ValueError):
                        remote_run = None
                if remote_run:
                    break
                time.sleep(5)
            if not remote_run:
                raise RuntimeError(
                    "Đã gửi lệnh nhưng không tìm thấy lượt chạy mới trong 3 phút. "
                    "Hãy mở tab Actions của repository để kiểm tra workflow có được kích hoạt không."
                )

            run_id = int(remote_run["id"])
            run_number = int(remote_run["run_number"])
            self.root.after(
                0, lambda j=job_id, m=f"GitHub Actions đang tạo audio · run #{run_number}...":
                self._set_audio_job_progress(j, m)
            )

            run_deadline = time.monotonic() + 6 * 60 * 60
            remote_status = "queued"
            while time.monotonic() < run_deadline:
                status_result = self._run_github_cli(
                    ["api", f"repos/{repo}/actions/runs/{run_id}"], timeout=45
                )
                if status_result.returncode != 0:
                    self.root.after(
                        0, lambda j=job_id, m="Đang chờ GitHub Actions phản hồi...":
                        self._set_audio_job_progress(j, m)
                    )
                    time.sleep(10)
                    continue
                try:
                    status_data = json.loads(status_result.stdout)
                except json.JSONDecodeError:
                    time.sleep(10)
                    continue
                remote_status = status_data.get("status", "queued")
                if remote_status == "completed":
                    conclusion = status_data.get("conclusion")
                    break
                self.root.after(
                    0, lambda j=job_id, m=f"GitHub Actions: {remote_status} · run #{run_number}...":
                    self._set_audio_job_progress(j, m)
                )
                time.sleep(10)
            else:
                raise RuntimeError(
                    "GitHub Actions vẫn chưa hoàn tất sau 6 giờ. Mở trang Actions để xem trạng thái run."
                )

            if conclusion != "success":
                logs = self._run_github_cli(
                    ["run", "view", str(run_id), "--repo", repo, "--log-failed"],
                    timeout=120,
                )
                log_text = (logs.stdout or "") + "\n" + (logs.stderr or "")
                log_text = log_text.strip()[-2600:]
                raise RuntimeError(
                    f"GitHub Actions kết thúc với trạng thái: {conclusion or 'không rõ'} "
                    f"(run #{run_number}).\n{log_text}"
                )

            self.root.after(
                0, lambda j=job_id, m="GitHub tạo xong · đang tải MP3 về máy...":
                self._set_audio_job_progress(j, m)
            )
            if download_dir.exists():
                shutil.rmtree(download_dir, ignore_errors=True)
            download_dir.mkdir(parents=True, exist_ok=True)
            downloaded = self._run_github_cli(
                [
                    "run", "download", str(run_id), "--repo", repo,
                    "--name", f"AU {run_number}", "--dir", str(download_dir),
                ],
                timeout=180,
            )
            if downloaded.returncode != 0:
                raise RuntimeError(
                    "GitHub đã tạo xong nhưng không tải được artifact MP3.\n"
                    + (downloaded.stderr or downloaded.stdout or "")[-1800:]
                )
            mp3_files = list(download_dir.rglob("*.mp3"))
            if not mp3_files:
                raise RuntimeError(
                    "Artifact của GitHub đã tải về nhưng không tìm thấy file MP3 bên trong."
                )

            AUDIO_DIR.mkdir(parents=True, exist_ok=True)
            safe_stem = self._safe_audio_stem(job["stem"])
            output_path = AUDIO_DIR / f"{safe_stem}_{job_id}.mp3"
            shutil.copy2(mp3_files[0], output_path)
            self.root.after(
                0, lambda j=job_id, p=output_path: self._complete_audio_job(j, p)
            )
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            self.root.after(
                0, lambda j=job_id, d=detail: self._fail_audio_job(j, d)
            )
        finally:
            if download_dir.exists():
                shutil.rmtree(download_dir, ignore_errors=True)
            upload_dir = TEMP_DIR / f"github_audio_upload_{job_id}"
            if upload_dir.exists():
                shutil.rmtree(upload_dir, ignore_errors=True)
            # Before dispatch, clean up a release if upload succeeded but dispatch failed.
            # After dispatch, the workflow's always()-cleanup job owns remote cleanup.
            if not locals().get("dispatched", False):
                tag = f"st-request-{request_id}"
                try:
                    self._run_github_cli(
                        ["release", "delete", tag, "--repo", repo, "--yes", "--cleanup-tag"],
                        timeout=60,
                    )
                except Exception:
                    pass

    @staticmethod
    def _safe_audio_stem(stem: str) -> str:
        # Remove Windows-invalid filename characters while preserving Vietnamese.
        invalid = set('<>:"/\\\\|?*')
        clean = "".join("_" if char in invalid or ord(char) < 32 else char for char in stem)
        return clean.strip(" .") or "audio"

    def _audio_worker(self, job: dict):
        job_id = job["id"]
        input_path = TEMP_DIR / f"long_tts_input_{job_id}.txt"
        safe_stem = self._safe_audio_stem(job["stem"])
        output_path = AUDIO_DIR / f"{safe_stem}_{job_id}.mp3"
        voice_ids = {
            "Nam Minh": "vi-VN-NamMinhNeural",
            "Hoài My": "vi-VN-HoaiMyNeural",
        }
        try:
            input_path.write_text(job["text"], encoding="utf-8")
            AUDIO_DIR.mkdir(parents=True, exist_ok=True)
            # Long chapters can take many minutes per full-text request. Do not
            # repeat the entire long request three times when the provider returns
            # no audio; stop after one attempt so the diagnostic log is available.
            attempts = "1" if len(job["text"]) >= 5000 else "3"
            proc = subprocess.run(
                [
                    str(AUDIO_PYTHON), str(AUDIO_TTS_GENERATOR),
                    "--input", str(input_path), "--output", str(output_path),
                    "--voice", voice_ids[job["voice"]], "--rate", job["rate"], "--attempts", attempts,
                ],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=None, cwd=str(ROOT_DIR),
            )
            if proc.returncode != 0:
                raise RuntimeError((proc.stderr or proc.stdout or "Bộ tạo audio dài trả về lỗi.").strip())
            if not output_path.is_file() or output_path.stat().st_size < 128:
                raise RuntimeError("File MP3 đầu ra rỗng hoặc không hợp lệ.")
            self.root.after(0, lambda j=job_id, p=output_path: self._complete_audio_job(j, p))
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            self.root.after(0, lambda j=job_id, d=detail: self._fail_audio_job(j, d))
        finally:
            input_path.unlink(missing_ok=True)

    def _complete_audio_job(self, job_id: str, path: Path):
        job = next((item for item in self.audio_history if item["id"] == job_id), None)
        if job is None:
            return
        job["state"] = "completed"
        job["path"] = str(path)
        job["error"] = None
        self._finish_audio_clock(job)
        self.audio_running = False
        self.audio_outputs.append(path)
        self.status.set(f"Audio hoàn tất: {path.name}")
        self._render_audio_status()
        if self.current_page == "audio":
            try:
                if self.audio_note.winfo_exists():
                    self.audio_note.configure(text=f"Đã tạo xong một file audio duy nhất: {path.name}")
                    self._render_audio_history()
                    messagebox.showinfo("Tạo audio thành công", f"Đã tạo một file MP3 hoàn chỉnh:\n{path}")
            except tk.TclError:
                pass

    def _fail_audio_job(self, job_id: str, detail: str):
        job = next((item for item in self.audio_history if item["id"] == job_id), None)
        if job is None:
            return
        job["state"] = "failed"
        job["error"] = detail
        self._finish_audio_clock(job)
        self.audio_running = False
        self.status.set("Tạo audio thất bại.")
        self._render_audio_status()
        if self.current_page == "audio":
            try:
                if self.audio_note.winfo_exists():
                    self.audio_note.configure(text="Tạo audio thất bại · xem chi tiết ở khung kết quả.")
                    self._render_audio_history()
                    messagebox.showerror("Không tạo được audio", detail[:3000])
            except tk.TclError:
                pass

    def open_selected_audio_output(self):
        try:
            index = self.audio_list.curselection()[0]
        except Exception:
            messagebox.showinfo("Chọn kết quả", "Hãy chọn một file audio đã tạo xong ở khung bên phải.")
            return
        path = self.audio_list_paths.get(index)
        if path and path.is_file():
            try:
                os.startfile(str(path))  # type: ignore[attr-defined]
            except Exception:
                webbrowser.open(path.as_uri())
        else:
            messagebox.showinfo(
                "Audio chưa sẵn sàng",
                "Dòng đang chọn chưa có MP3 hoàn chỉnh. Hãy chọn dòng bắt đầu bằng “✓ ĐÃ XONG”."
            )

    def open_folder(self, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(folder))  # type: ignore[attr-defined]
        except Exception:
            messagebox.showinfo("Thư mục", str(folder))

def main():
    root = tk.Tk()
    app = NovelStudioApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
