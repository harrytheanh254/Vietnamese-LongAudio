from __future__ import annotations

import argparse
import asyncio
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

VOICE_OPTIONS = {
    "vi-VN-NamMinhNeural": "Nam Minh",
    "vi-VN-HoaiMyNeural": "Hoài My",
}
RATE_OPTIONS = ("0.75", "1.0", "1.25", "1.5", "1.75", "2.0")
BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent.parent
DEFAULT_INPUT = BASE_DIR / "kichban.txt"
DEFAULT_OUTPUT = BASE_DIR / "outputs" / "audio_youtube.mp3"
PLACEHOLDER = "__DAN_NOI_DUNG_TRUYEN_VAO_DAY__"
CHUNK_CHARS = 1800
MIN_RECOVERY_CHARS = 100


def rate_to_edge(rate: str) -> str:
    if rate not in RATE_OPTIONS:
        raise ValueError("Tốc độ đọc không hợp lệ.")
    percent = round((float(rate) - 1.0) * 100)
    return f"{percent:+d}%"


def read_text_file(path: str | Path) -> str:
    """Read the complete UTF-8 input file without an app-level character cap."""
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except UnicodeError as exc:
        raise ValueError(
            f"Không đọc được {source} dưới dạng UTF-8. Hãy lưu tệp bằng UTF-8."
        ) from exc

    if not text.strip():
        raise ValueError(f"Tệp đầu vào đang trống: {source}")
    if text.strip() == PLACEHOLDER:
        raise ValueError(
            "Hãy thay dòng hướng dẫn trong kichban.txt bằng nội dung truyện trước khi chạy."
        )
    return text


def configure_websocket_endpoint() -> None:
    """Use Edge-TTS's upstream endpoint unless an explicit override is supplied."""
    try:
        import edge_tts.communicate as edge_communicate
    except ImportError as exc:
        raise RuntimeError(
            "Chưa cài edge-tts. Hãy chạy: python -m pip install -r requirements.txt"
        ) from exc

    override = os.environ.get("EDGE_TTS_WEBSOCKET_URL", "").strip()
    if override and override.lower() != "default":
        edge_communicate.WSS_URL = override


def split_text(text: str, max_chars: int = CHUNK_CHARS) -> list[str]:
    """Split at natural boundaries while preserving every source character."""
    if max_chars < 200:
        raise ValueError("Kích thước đoạn audio phải từ 200 ký tự trở lên.")
    chunks: list[str] = []
    start = 0
    preferred_breaks = ("\n", ".", "!", "?", "。", "！", "？", ";", "；", " ")
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end == len(text):
            chunks.append(text[start:end])
            break

        window = text[start:end]
        floor = int(max_chars * 0.55)
        candidates = [
            window.rfind(mark) + 1
            for mark in preferred_breaks
            if window.rfind(mark) >= floor
        ]
        cut = max(candidates) if candidates else len(window)
        if cut <= 0:
            cut = len(window)
        chunks.append(text[start:start + cut])
        start += cut
    return [chunk for chunk in chunks if chunk.strip()]


def split_failed_chunk(text: str) -> tuple[str, str]:
    """Split a failed chunk near its middle, preferably between sentences/words."""
    middle = len(text) // 2
    low, high = int(len(text) * 0.30), int(len(text) * 0.70)
    break_chars = set("\n .!?。！？；;，,")
    candidates = [i + 1 for i in range(low, min(high, len(text))) if text[i] in break_chars]
    if candidates:
        cut = min(candidates, key=lambda point: abs(point - middle))
    else:
        cut = middle
    cut = max(1, min(cut, len(text) - 1))
    return text[:cut], text[cut:]


async def stream_audio_bytes(
    text: str,
    voice: str,
    edge_rate: str,
    sink=None,
) -> int:
    """Stream one request; return audio byte count and optionally append bytes to sink."""
    import edge_tts

    received = 0
    communicator = edge_tts.Communicate(text=text, voice=voice, rate=edge_rate)
    async for message in communicator.stream():
        if message.get("type") != "audio":
            continue
        data = message.get("data") or b""
        if not data:
            continue
        if sink is not None:
            sink.write(data)
        received += len(data)
    if received == 0:
        raise RuntimeError("Edge-TTS kết thúc yêu cầu nhưng không trả về dữ liệu âm thanh.")
    return received


async def verify_voice_service(voice: str, edge_rate: str, attempts: int) -> None:
    """Check the selected voice with a tiny phrase before processing a long story."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            await stream_audio_bytes("Xin chào.", voice, edge_rate)
            print("Kiểm tra giọng đọc: thành công.", flush=True)
            return
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                delay = (2, 5)[min(attempt - 1, 1)]
                print(
                    f"Kiểm tra giọng đọc lần {attempt}/{attempts} chưa thành công: {exc}. "
                    f"Thử lại sau {delay} giây...",
                    file=sys.stderr,
                    flush=True,
                )
                await asyncio.sleep(delay)

    raise RuntimeError(
        "Dịch vụ Edge-TTS không trả âm thanh ngay cả với câu thử rất ngắn. "
        "Khả năng cao dịch vụ/giọng đọc hoặc kết nối mạng đang gặp vấn đề, chưa phải do truyện quá dài. "
        "Hãy kiểm tra Internet, thử giọng còn lại hoặc thử lại sau. "
        f"Chi tiết: {last_error}"
    ) from last_error


async def write_chunk_with_recovery(
    text: str,
    voice: str,
    edge_rate: str,
    sink,
    attempts: int,
    label: str,
    depth: int = 0,
) -> int:
    """Write a chunk into the final MP3, retrying and subdividing failures if needed."""
    start_position = sink.tell()
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        sink.seek(start_position)
        sink.truncate()
        try:
            byte_count = await stream_audio_bytes(text, voice, edge_rate, sink)
            if byte_count == 0:
                raise RuntimeError("Không nhận được dữ liệu âm thanh.")
            return byte_count
        except Exception as exc:
            last_error = exc
            sink.seek(start_position)
            sink.truncate()
            if attempt < attempts:
                delay = (2, 5)[min(attempt - 1, 1)]
                print(
                    f"Đoạn {label}, lần thử {attempt}/{attempts} thất bại: {exc}. "
                    f"Thử lại sau {delay} giây...",
                    file=sys.stderr,
                    flush=True,
                )
                await asyncio.sleep(delay)

    # A shorter request may work when a long utterance contains a troublesome
    # expression or the service fails on one particular sub-request.
    if len(text.strip()) > MIN_RECOVERY_CHARS and depth < 6:
        left, right = split_failed_chunk(text)
        print(
            f"Đoạn {label} vẫn lỗi; tự tách nhỏ hơn và tiếp tục ghép vào cùng một MP3.",
            file=sys.stderr,
            flush=True,
        )
        first_bytes = await write_chunk_with_recovery(
            left, voice, edge_rate, sink, attempts, f"{label}.a", depth + 1
        )
        second_bytes = await write_chunk_with_recovery(
            right, voice, edge_rate, sink, attempts, f"{label}.b", depth + 1
        )
        return first_bytes + second_bytes

    preview = " ".join(text.split())[:180]
    raise RuntimeError(
        f"Không tạo được audio cho một đoạn ngắn ({label}). "
        f"Đoạn văn liên quan: {preview!r}. Lỗi cuối: {last_error}. "
        "Nội dung chưa bị tự ý bỏ qua; hãy sửa đoạn này hoặc thử lại sau."
    ) from last_error


async def synthesize_one_audio(
    text: str,
    voice: str,
    rate: str,
    output_path: str | Path,
    attempts: int = 3,
) -> Path:
    """Generate one MP3 by streaming small requests into a single output file."""
    if voice not in VOICE_OPTIONS:
        raise ValueError("Giọng đọc không hợp lệ.")
    edge_rate = rate_to_edge(rate)
    if attempts < 1:
        raise ValueError("Số lần thử phải ít nhất là 1.")

    try:
        import edge_tts  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "Chưa cài edge-tts. Hãy chạy: python -m pip install -r requirements.txt"
        ) from exc

    configure_websocket_endpoint()
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.stem + ".partial" + destination.suffix)
    partial.unlink(missing_ok=True)

    try:
        # Fail early when the chosen voice or Microsoft endpoint is not responding.
        await verify_voice_service(voice, edge_rate, attempts)
        chunks = split_text(text)
        total_chunks = len(chunks)
        if not total_chunks:
            raise ValueError("Không có nội dung đọc sau khi tách văn bản.")

        total_bytes = 0
        with partial.open("wb") as sink:
            for index, chunk in enumerate(chunks, start=1):
                added = await write_chunk_with_recovery(
                    chunk, voice, edge_rate, sink, attempts, str(index)
                )
                total_bytes += added
                print(
                    f"[PROGRESS] {index}/{total_chunks} đoạn đã tạo.",
                    flush=True,
                )
            sink.flush()
            os.fsync(sink.fileno())

        if not partial.is_file() or partial.stat().st_size < 128 or total_bytes == 0:
            raise RuntimeError("Edge-TTS không tạo được tệp MP3 hợp lệ.")
        os.replace(partial, destination)
        return destination
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def resolve_from_project(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate
    return (BASE_DIR / candidate).resolve()


def write_error_log(exc: BaseException) -> Path | None:
    try:
        log_path = PROJECT_DIR / "logs" / "audio_error.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            log.write("\n" + "=" * 72 + "\n")
            log.write(f"Thời điểm: {datetime.now().isoformat(timespec='seconds')}\n")
            log.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        return log_path
    except OSError:
        return None


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Tạo một MP3 hoàn chỉnh từ toàn bộ tệp TXT bằng Edge-TTS."
    )
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="Tệp TXT UTF-8.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Đường dẫn MP3 đầu ra.")
    parser.add_argument("--voice", choices=sorted(VOICE_OPTIONS),
                        default="vi-VN-NamMinhNeural")
    parser.add_argument("--rate", choices=RATE_OPTIONS, default="1.25")
    parser.add_argument("--attempts", type=int, default=3)
    return parser


def main() -> int:
    args = make_parser().parse_args()
    try:
        text = read_text_file(resolve_from_project(args.input))
        output = resolve_from_project(args.output)
        print(f"Tệp đầu vào: {args.input}")
        print(f"Số ký tự: {len(text):,}")
        print(f"Giọng đọc: {VOICE_OPTIONS[args.voice]} ({args.voice})")
        print(f"Tốc độ: {args.rate}x")
        print("Đang tạo một MP3 duy nhất; các đoạn nhỏ chỉ dùng nội bộ để tăng độ ổn định.")
        result = asyncio.run(
            synthesize_one_audio(text, args.voice, args.rate, output, args.attempts)
        )
        print("\nTẠO AUDIO THÀNH CÔNG")
        print(f"MP3: {result}")
        print(f"Dung lượng: {result.stat().st_size:,} byte")
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        log_path = write_error_log(exc)
        print(f"\nLỖI: {exc}", file=sys.stderr)
        if log_path:
            print(f"Nhật ký chi tiết: {log_path}", file=sys.stderr)
        return 1


if __import__("sys").version_info >= (3, 8) and __name__ == "__main__":
    raise SystemExit(main())
