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


async def synthesize_one_audio(
    text: str,
    voice: str,
    rate: str,
    output_path: str | Path,
    attempts: int = 3,
) -> Path:
    """Generate one MP3 with one whole-script Edge-TTS request per attempt.

    This intentionally matches the fast path used by GitHub Actions: Edge-TTS
    handles its protocol-level text streaming internally, while the application
    makes just one request for the whole chapter and writes one output MP3.
    """
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
                # Important for speed: submit the chapter as one Communicate job.
                communicator = edge_tts.Communicate(
                    text=text,
                    voice=voice,
                    rate=edge_rate,
                )
                await communicator.save(str(partial))
                if not partial.is_file() or partial.stat().st_size == 0:
                    raise RuntimeError("Edge-TTS không tạo được tệp MP3 hợp lệ.")
                os.replace(partial, destination)
                print(
                    f"Đã tạo xong một MP3 từ toàn bộ văn bản ({len(text):,} ký tự).",
                    flush=True,
                )
                return destination
            except Exception as exc:
                last_error = exc
                partial.unlink(missing_ok=True)
                if attempt < attempts:
                    delay = (3, 8)[min(attempt - 1, 1)]
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


if __name__ == "__main__":
    raise SystemExit(main())
