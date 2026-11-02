import json
from pathlib import Path

import app as web_app


class FakeQueue:
    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


def setup_test_db(tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "DB_PATH", tmp_path / "test.sqlite3")
    monkeypatch.setattr(web_app, "DATA_DIR", tmp_path)
    monkeypatch.setattr(web_app, "AUDIO_DIR", tmp_path / "audio")
    fake_queue = FakeQueue()
    monkeypatch.setattr(web_app, "job_queue", fake_queue)
    web_app.initialize_db()
    return fake_queue


def test_create_jobs_assign_numbers_when_submitted(tmp_path: Path, monkeypatch):
    fake_queue = setup_test_db(tmp_path, monkeypatch)
    client = web_app.app.test_client()
    payload = {
        "content": "Đoạn truyện thử nghiệm.",
        "voice": "vi-VN-NamMinhNeural",
        "rate": "1.25",
    }

    first = client.post("/api/jobs", data=json.dumps(payload), content_type="application/json")
    second = client.post("/api/jobs", data=json.dumps(payload), content_type="application/json")

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.get_json()["job"]["label"] == "AU 1"
    assert second.get_json()["job"]["label"] == "AU 2"
    assert fake_queue.items == [1, 2]

    history = client.get("/api/jobs").get_json()["jobs"]
    assert [job["label"] for job in history] == ["AU 2", "AU 1"]
    assert all(job["status"] == "queued" for job in history)
    assert all(job["segment_count"] == 0 for job in history)


def test_create_job_rejects_missing_text_and_invalid_options(tmp_path: Path, monkeypatch):
    fake_queue = setup_test_db(tmp_path, monkeypatch)
    client = web_app.app.test_client()

    missing_text = client.post(
        "/api/jobs",
        json={"content": "  ", "voice": "vi-VN-NamMinhNeural", "rate": "1.25"},
    )
    invalid_voice = client.post(
        "/api/jobs",
        json={"content": "Nội dung", "voice": "invalid", "rate": "1.25"},
    )

    assert missing_text.status_code == 400
    assert invalid_voice.status_code == 400
    assert fake_queue.items == []


def test_process_job_splits_text_creates_segment_rows_and_combined_audio(
    tmp_path: Path, monkeypatch
):
    setup_test_db(tmp_path, monkeypatch)
    client = web_app.app.test_client()
    text = "Đây là nội dung truyện tiếng Việt. " * 400
    created = client.post(
        "/api/jobs",
        json={
            "content": text,
            "voice": "vi-VN-NamMinhNeural",
            "rate": "1.25",
        },
    )
    assert created.status_code == 201
    job_id = created.get_json()["job"]["id"]

    async def fake_chunk_synthesis(
        *, text, voice, rate, output_path, segment_dir, max_chars,
        concurrency, attempts, status_callback
    ):
        chunks = web_app.split_text_into_chunks(text, max_chars)
        segment_dir = Path(segment_dir)
        segment_dir.mkdir(parents=True, exist_ok=True)
        part_paths = []
        for index, chunk in enumerate(chunks, start=1):
            status_callback(index, "generating", None, None)
            path = segment_dir / f"part-{index:04d}.mp3"
            path.write_bytes(b"segment-audio-" + str(index).encode())
            status_callback(index, "completed", None, path)
            part_paths.append(path)
        Path(output_path).write_bytes(b"".join(path.read_bytes() for path in part_paths))
        return Path(output_path), chunks, part_paths

    monkeypatch.setattr(web_app, "synthesize_text_in_chunks", fake_chunk_synthesis)
    web_app.process_job(job_id)

    history = client.get("/api/jobs").get_json()["jobs"]
    job = history[0]
    assert job["status"] == "completed"
    assert job["segment_count"] == 3
    assert job["completed_segments"] == 3
    assert [segment["index"] for segment in job["segments"]] == [1, 2, 3]
    assert all(segment["status"] == "completed" for segment in job["segments"])
    assert Path(web_app.get_job(job_id)["audio_path"]).is_file()

    final_audio = client.get(f"/api/jobs/{job_id}/audio?download=1")
    assert final_audio.status_code == 200
    assert f"AU {job_id}.mp3" in final_audio.headers["Content-Disposition"]

    part_audio = client.get(f"/api/jobs/{job_id}/segments/2/audio?download=1")
    assert part_audio.status_code == 200
    assert "Doan 2.mp3" in part_audio.headers["Content-Disposition"]


def test_failed_segment_marks_parent_failed_but_keeps_segment_list(
    tmp_path: Path, monkeypatch
):
    setup_test_db(tmp_path, monkeypatch)
    client = web_app.app.test_client()
    created = client.post(
        "/api/jobs",
        json={
            "content": "Nội dung ngắn để kiểm tra lỗi.",
            "voice": "vi-VN-NamMinhNeural",
            "rate": "1.0",
        },
    )
    job_id = created.get_json()["job"]["id"]

    async def fail_chunk_synthesis(
        *, text, voice, rate, output_path, segment_dir, max_chars,
        concurrency, attempts, status_callback
    ):
        status_callback(1, "generating", None, None)
        status_callback(1, "failed", "Dịch vụ tạm thời lỗi", None)
        raise web_app.ChunkSynthesisError([(1, "Dịch vụ tạm thời lỗi")])

    monkeypatch.setattr(web_app, "synthesize_text_in_chunks", fail_chunk_synthesis)
    web_app.process_job(job_id)

    job = client.get("/api/jobs").get_json()["jobs"][0]
    assert job["status"] == "failed"
    assert job["failed_segments"] == 1
    assert job["segments"][0]["status"] == "failed"
    assert "Dịch vụ tạm thời lỗi" in job["error"]
