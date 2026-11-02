from __future__ import annotations

import argparse
import asyncio
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

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
CHUNK_CHARS = 5000
MAX_CONCURRENT_SEGMENTS = 30


class ChunkSynthesisError(RuntimeError):
    def __init__(self, failed_segments: list[tuple[int, str]]):
        self.failed_segments = failed_segments
        details = "; ".join(
            f"đoạn {index}: {message}" for index, message in failed_segments[:8]
        )
        if len(failed_segments) > 8:
            details += f"; và {len(failed_segments) - 8} đoạn khác"
        super().__init__(f"{len(failed_segments)} đoạn không tạo được audio. {details}")


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


def split_text_into_chunks(text: str, max_chars: int = CHUNK_CHARS) -> list[str]:
    """Split text while preserving every character and preferring whitespace breaks."""
    if max_chars < 1:
        raise ValueError("Kích thước mỗi đoạn phải lớn hơn 0.")
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            search_from = start + max(1, max_chars * 3 // 4)
            boundary = max(
                text.rfind(" ", search_from, end),
                text.rfind("\n", search_from, end),
                text.rfind("\t", search_from, end),
                text.rfind("\r", search_from, end),
            )
            if boundary >= search_from:
                end = boundary + 1
        chunks.append(text[start:end])
        start = end
    return chunks


def _strip_mp3_tags(data: bytes) -> bytes:
    """Remove ID3 metadata blocks before joining MP3 frame streams."""
    if data.startswith(b"ID3") and len(data) >= 10:
        size = (
            ((data[6] & 0x7F) << 21)
            | ((data[7] & 0x7F) << 14)
            | ((data[8] & 0x7F) << 7)
            | (data[9] & 0x7F)
        )
        footer_size = 10 if data[5] & 0x10 else 0
        data = data[10 + size + footer_size :]
    if len(data) >= 128 and data[-128:-125] == b"TAG":
        data = data[:-128]
    return data


def concatenate_mp3_files(
    input_paths: Iterable[str | Path], output_path: str | Path
) -> Path:
    """Join ordered MP3 streams without re-encoding or reducing audio quality."""
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.stem + ".partial" + destination.suffix)
    paths = [Path(path) for path in input_paths]
    if not paths:
        raise ValueError("Không có đoạn audio nào để ghép.")
    try:
        with partial.open("wb") as output:
            for index, path in enumerate(paths, start=1):
                if not path.is_file():
                    raise FileNotFoundError(f"Không tìm thấy audio đoạn {index}: {path}")
                payload = _strip_mp3_tags(path.read_bytes())
                if not payload:
                    raise RuntimeError(f"Audio đoạn {index} đang trống hoặc không hợp lệ.")
                output.write(payload)
        if not partial.is_file() or partial.stat().st_size == 0:
            raise RuntimeError("Không tạo được tệp MP3 tổng hợp.")
        os.replace(partial, destination)
        return destination
    except Exception:
        partial.unlink(missing_ok=True)
        raise


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


async def synthesize_one_audio(
    text: str,
    voice: str,
    rate: str,
    output_path: str | Path,
    attempts: int = 3,
) -> Path:
    """Generate one MP3 part from one text chunk using Edge-TTS."""
    if voice not in VOICE_OPTIONS:
        raise ValueError("Giọng đọc không hợp lệ.")
    edge_rate = rate_to_edge(rate)
    if attempts < 1:
        raise ValueError("Số lần thử phải ít nhất là 1.")
    if not text.strip():
        raise ValueError("Nội dung cần đọc đang trống.")
    try:
        import edge_tts
    except ImportError as exc:
        raise RuntimeError(
            "Chưa cài edge-tts. Hãy chạy: python -m pip install -r requirements.txt"
        ) from exc

    configure_websocket_endpoint()
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.stem + ".partial" + destination.suffix)
    last_error: Exception | None = None
    try:
        for attempt in range(1, attempts + 1):
            partial.unlink(missing_ok=True)
            try:
                communicator = edge_tts.Communicate(
                    text=text, voice=voice, rate=edge_rate
                )
                await communicator.save(str(partial))
                if not partial.is_file() or partial.stat().st_size == 0:
                    raise RuntimeError("Edge-TTS không tạo được tệp MP3 hợp lệ.")
                os.replace(partial, destination)
                return destination
            except Exception as exc:
                last_error = exc
                partial.unlink(missing_ok=True)
                if attempt < attempts:
                    delay = (4, 12)[min(attempt - 1, 1)]
                    print(
                        f"Lần thử {attempt}/{attempts} thất bại: {exc}. "
                        f"Thử lại sau {delay} giây...",
                        file=sys.stderr,
                        flush=True,
                    )
                    await asyncio.sleep(delay)
        raise RuntimeError(
            f"Không tạo được audio sau {attempts} lần thử: {last_error}"
        ) from last_error
    except Exception:
        partial.unlink(missing_ok=True)
        raise


async def synthesize_text_in_chunks(
    text: str,
    voice: str,
    rate: str,
    output_path: str | Path,
    *,
    segment_dir: str | Path | None = None,
    max_chars: int = CHUNK_CHARS,
    concurrency: int = MAX_CONCURRENT_SEGMENTS,
    attempts: int = 3,
    status_callback: Callable[[int, str, str | None, Path | None], None] | None = None,
) -> tuple[Path, list[str], list[Path]]:
    """Synthesize up to 30 chunks at once, then join them in source order."""
    if voice not in VOICE_OPTIONS:
        raise ValueError("Giọng đọc không hợp lệ.")
    rate_to_edge(rate)
    chunks = split_text_into_chunks(text, max_chars)
    if not chunks:
        raise ValueError("Nội dung cần chuyển thành audio đang trống.")
    output = Path(output_path)
    work_dir = Path(segment_dir) if segment_dir else output.with_name(output.stem + "_parts")
    work_dir.mkdir(parents=True, exist_ok=True)
    paths = [
        work_dir / f"part-{index:04d}.mp3"
        for index in range(1, len(chunks) + 1)
    ]
    semaphore = asyncio.Semaphore(max(1, min(int(concurrency), MAX_CONCURRENT_SEGMENTS)))
    failures: list[tuple[int, str]] = []

    async def synthesize_part(index: int, part_text: str, part_path: Path) -> None:
        async with semaphore:
            if status_callback:
                status_callback(index, "generating", None, None)
            try:
                await synthesize_one_audio(
                    text=part_text, voice=voice, rate=rate,
                    output_path=part_path, attempts=attempts,
                )
            except Exception as exc:
                message = str(exc).strip() or exc.__class__.__name__
                failures.append((index, message))
                if status_callback:
                    status_callback(index, "failed", message, None)
            else:
                if status_callback:
                    status_callback(index, "completed", None, part_path)

    await asyncio.gather(*(
        synthesize_part(index, part_text, paths[index - 1])
        for index, part_text in enumerate(chunks, start=1)
    ))
    if failures:
        raise ChunkSynthesisError(sorted(failures))
    combined = concatenate_mp3_files(paths, output)
    return combined, chunks, paths


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
        description="Chia văn bản thành đoạn 5.000 ký tự, tạo tối đa 30 đoạn đồng thời và ghép thành một MP3."
    )
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="Tệp TXT UTF-8.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Đường dẫn MP3 đầu ra.")
    parser.add_argument("--voice", choices=sorted(VOICE_OPTIONS), default="vi-VN-NamMinhNeural")
    parser.add_argument("--rate", choices=RATE_OPTIONS, default="1.25")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--chunk-size", type=int, default=CHUNK_CHARS)
    parser.add_argument("--concurrency", type=int, default=MAX_CONCURRENT_SEGMENTS)
    return parser


def main() -> int:
    args = make_parser().parse_args()
    try:
        text = read_text_file(resolve_from_project(args.input))
        output = resolve_from_project(args.output)
        chunks = split_text_into_chunks(text, args.chunk_size)
        print(f"Tệp đầu vào: {args.input}")
        print(f"Số ký tự: {len(text):,}")
        print(f"Số đoạn: {len(chunks):,} (tối đa {args.chunk_size:,} ký tự/đoạn)")
        print(f"Số đoạn xử lý đồng thời: {max(1, min(args.concurrency, MAX_CONCURRENT_SEGMENTS))}")
        print(f"Giọng đọc: {VOICE_OPTIONS[args.voice]} ({args.voice})")
        print(f"Tốc độ: {args.rate}x")
        result, _, _ = asyncio.run(
            synthesize_text_in_chunks(
                text=text, voice=args.voice, rate=args.rate, output_path=output,
                segment_dir=output.with_name(output.stem + "_parts"),
                max_chars=args.chunk_size, concurrency=args.concurrency,
                attempts=args.attempts,
            )
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


if __name__ == "__main__":
    raise SystemExit(main())
