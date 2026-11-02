import asyncio
from pathlib import Path

import edge_tts
import pytest

import generate_audio


@pytest.mark.parametrize(
    ("multiplier", "expected"),
    [("0.75", "-25%"), ("1.0", "+0%"), ("1.25", "+25%"),
     ("1.5", "+50%"), ("1.75", "+75%")],
)
def test_rate_mapping(multiplier, expected):
    assert generate_audio.rate_to_edge(multiplier) == expected


def test_reader_accepts_long_utf8_bom_text_without_app_limit(tmp_path: Path):
    source = tmp_path / "kichban.txt"
    expected = ("Tiếng Việt, hội thoại và dấu câu.\n" * 250_000)
    source.write_text("\ufeff" + expected, encoding="utf-8")
    assert generate_audio.read_text_file(source) == expected


def test_reader_rejects_empty_and_placeholder_files(tmp_path: Path):
    source = tmp_path / "kichban.txt"
    source.write_text("  \n", encoding="utf-8")
    with pytest.raises(ValueError, match="trống"):
        generate_audio.read_text_file(source)
    source.write_text(generate_audio.PLACEHOLDER, encoding="utf-8")
    with pytest.raises(ValueError, match="thay dòng hướng dẫn"):
        generate_audio.read_text_file(source)


def test_splitter_preserves_all_text_and_respects_character_limit():
    text = ("Đây là một câu chuyện tiếng Việt có dấu. " * 500) + "\nKết thúc."
    chunks = generate_audio.split_text_into_chunks(text, max_chars=5000)
    assert len(chunks) > 1
    assert all(0 < len(chunk) <= 5000 for chunk in chunks)
    assert "".join(chunks) == text


def test_splitter_handles_unbroken_text_and_custom_limit():
    text = "A" * 12001
    chunks = generate_audio.split_text_into_chunks(text, max_chars=5000)
    assert [len(chunk) for chunk in chunks] == [5000, 5000, 2001]
    assert "".join(chunks) == text


def test_mp3_concatenation_preserves_order_and_strips_id3(tmp_path: Path):
    first = tmp_path / "part-1.mp3"
    second = tmp_path / "part-2.mp3"
    output = tmp_path / "final.mp3"
    first.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"FIRST")
    second.write_bytes(b"SECOND")

    result = generate_audio.concatenate_mp3_files([first, second], output)

    assert result == output
    assert output.read_bytes() == b"FIRSTSECOND"


def test_chunks_are_synthesized_with_bounded_parallelism_and_joined_in_order(
    tmp_path: Path, monkeypatch
):
    active = 0
    max_active = 0
    callbacks = []

    async def fake_synthesis(*, text, voice, rate, output_path, attempts):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(text.encode("utf-8"))
        active -= 1
        return output

    monkeypatch.setattr(generate_audio, "synthesize_one_audio", fake_synthesis)
    text = "0123456789" * 1200
    output = tmp_path / "final.mp3"

    result, chunks, paths = asyncio.run(
        generate_audio.synthesize_text_in_chunks(
            text=text,
            voice="vi-VN-NamMinhNeural",
            rate="1.0",
            output_path=output,
            segment_dir=tmp_path / "parts",
            concurrency=30,
            status_callback=lambda *args: callbacks.append(args),
        )
    )

    assert result == output
    assert len(chunks) == 3
    assert len(paths) == 3
    assert max_active <= 30
    assert "".join(path.read_bytes().decode("utf-8") for path in paths) == text
    assert output.read_bytes() == text.encode("utf-8")
    assert [item[1] for item in callbacks].count("completed") == 3


def test_chunk_synthesis_failure_reports_failed_parts_and_keeps_other_results(
    tmp_path: Path, monkeypatch
):
    async def fake_synthesis(*, text, voice, rate, output_path, attempts):
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        if text.startswith("FAIL"):
            raise RuntimeError("mock service error")
        output.write_bytes(b"valid-mp3-frame")
        return output

    monkeypatch.setattr(generate_audio, "synthesize_one_audio", fake_synthesis)
    with pytest.raises(generate_audio.ChunkSynthesisError) as exc:
        asyncio.run(
            generate_audio.synthesize_text_in_chunks(
                text=("OK" * 2500) + ("FAIL" * 1500) + ("END" * 100),
                voice="vi-VN-NamMinhNeural",
                rate="1.0",
                output_path=tmp_path / "final.mp3",
                segment_dir=tmp_path / "parts",
                max_chars=5000,
                concurrency=30,
            )
        )
    assert exc.value.failed_segments
    assert not (tmp_path / "final.mp3").exists()


def test_one_chunk_uses_edge_tts_and_writes_mp3(tmp_path: Path, monkeypatch):
    calls = []

    class FakeCommunicate:
        def __init__(self, *, text, voice, rate):
            calls.append({"text": text, "voice": voice, "rate": rate})

        async def save(self, destination):
            Path(destination).write_bytes(b"mock-mp3-data")

    monkeypatch.setattr(edge_tts, "Communicate", FakeCommunicate)
    monkeypatch.setattr(generate_audio, "configure_websocket_endpoint", lambda: None)
    text = "Một đoạn truyện thử nghiệm."
    output = tmp_path / "part.mp3"

    result = asyncio.run(
        generate_audio.synthesize_one_audio(
            text, "vi-VN-HoaiMyNeural", "1.25", output
        )
    )

    assert result == output
    assert output.read_bytes() == b"mock-mp3-data"
    assert calls == [{
        "text": text, "voice": "vi-VN-HoaiMyNeural", "rate": "+25%"
    }]
    assert not (tmp_path / "part.partial.mp3").exists()


def test_failed_synthesis_retries_one_chunk(tmp_path: Path, monkeypatch):
    seen = []

    class SometimesFails:
        def __init__(self, *, text, voice, rate):
            seen.append(text)

        async def save(self, destination):
            if len(seen) == 1:
                Path(destination).write_bytes(b"partial")
                raise RuntimeError("temporary service error")
            Path(destination).write_bytes(b"complete-mp3")

    async def no_wait(_):
        return None

    monkeypatch.setattr(edge_tts, "Communicate", SometimesFails)
    monkeypatch.setattr(generate_audio, "configure_websocket_endpoint", lambda: None)
    monkeypatch.setattr(generate_audio.asyncio, "sleep", no_wait)
    output = tmp_path / "part.mp3"
    source_text = "Một phần của kịch bản."

    result = asyncio.run(
        generate_audio.synthesize_one_audio(
            source_text, "vi-VN-NamMinhNeural", "1.0", output
        )
    )

    assert seen == [source_text, source_text]
    assert result.read_bytes() == b"complete-mp3"


def test_failure_after_max_attempts_cleans_partial_file(tmp_path: Path, monkeypatch):
    calls = []

    class AlwaysFails:
        def __init__(self, *, text, voice, rate):
            calls.append(text)

        async def save(self, destination):
            Path(destination).write_bytes(b"partial")
            raise RuntimeError("service unavailable")

    async def no_wait(_):
        return None

    monkeypatch.setattr(edge_tts, "Communicate", AlwaysFails)
    monkeypatch.setattr(generate_audio, "configure_websocket_endpoint", lambda: None)
    monkeypatch.setattr(generate_audio.asyncio, "sleep", no_wait)
    output = tmp_path / "part.mp3"

    with pytest.raises(RuntimeError, match="sau 2 lần thử"):
        asyncio.run(
            generate_audio.synthesize_one_audio(
                "Nội dung thử", "vi-VN-NamMinhNeural", "1.0", output, attempts=2
            )
        )

    assert len(calls) == 2
    assert not output.exists()
    assert not (tmp_path / "part.partial.mp3").exists()
