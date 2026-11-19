"""Bounded audio extraction, model reuse and safe concurrent local inference."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

import transcription
from autoclips import transcript_local


def _word(start, end, text):
    return SimpleNamespace(start=start, end=end, word=text)


def _segment(words, end=None):
    return SimpleNamespace(words=words, end=end if end is not None else words[-1].end)


@pytest.fixture
def mock_source(tmp_path, monkeypatch):
    source = tmp_path / "finished.mp4"
    source.write_bytes(b"test-media")
    monkeypatch.setattr(transcription, "_MODEL", None)
    monkeypatch.setattr(transcription, "_MODEL_KEY", None)
    monkeypatch.setenv("AUTOCLIPS_WHISPER_CACHE", str(tmp_path / "models"))
    monkeypatch.setattr(transcription, "_audio_duration", lambda source: 2.)
    monkeypatch.setattr(transcription, "_extract_chunk",
                        lambda src, dest, offset, duration: dest.write_bytes(b"wav"))
    monkeypatch.setattr(transcription, "_read_audio_chunk", lambda path: str(path))
    return source


def test_chunks_bound_memory_rebase_words_preserve_language_and_report_progress(mock_source, monkeypatch):
    monkeypatch.setattr(transcription, "_audio_duration", lambda source: 361.2)
    extracts, calls, created, progress = [], [], [], []
    chunk_words = [
        [_word(.2, .6, " Olá"), _word(179.8, 180.2, "borda")],
        [_word(.8, 1.2, "borda"), _word(180.5, 181., "segundo"), _word(181., 181.5, "fim")],
        [_word(1., 1.5, "fim"), _word(2.1, 2.8, "fora")],
    ]

    class FakeModel:
        def __init__(self, model, **kwargs):
            created.append((model, kwargs))

        def transcribe(self, path, **kwargs):
            assert Path(path).read_bytes() == b"bounded-wav"
            calls.append(kwargs)
            return iter([_segment(chunk_words[len(calls) - 1])]), SimpleNamespace(language="pt")

    def extract(src, destination, offset, duration):
        extracts.append((destination, offset, duration))
        destination.write_bytes(b"bounded-wav")

    monkeypatch.setattr(transcription, "_extract_chunk", extract)
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeModel))
    result = transcript_local(mock_source, "tiny", progress=progress.append)
    assert [(offset, duration) for _, offset, duration in extracts] == [
        (0., 181.), (179., 182.), (359., pytest.approx(2.2))]
    assert [word["text"] for word in result["words"]] == ["Olá", "borda", "segundo", "fim"]
    assert result["words"][1]["start"] == 179.8
    assert result["words"][-1]["start"] == 360.
    assert calls[0]["language"] is None and calls[1]["language"] == "pt"
    assert result["language"] == "pt"
    assert created[0][1]["device"] == "cpu" and created[0][1]["compute_type"] == "int8"
    assert all(not path.exists() and not path.parent.exists() for path, _, _ in extracts)
    assert progress[-1]["phase"] == "done" and progress[-1]["progress"] == 100
    assert [item["progress"] for item in progress] == sorted(item["progress"] for item in progress)


def test_model_is_reused_and_lazy_generators_are_serialized(mock_source, monkeypatch):
    loaded = []
    concurrent = {"active": 0, "peak": 0}
    guard = threading.Lock()

    class FakeModel:
        def __init__(self, model, **kwargs):
            loaded.append(model)

        def transcribe(self, path, **kwargs):
            def segments():
                with guard:
                    concurrent["active"] += 1
                    concurrent["peak"] = max(concurrent["peak"], concurrent["active"])
                try:
                    time.sleep(.02)
                    yield _segment([_word(.1, 1., "fala")])
                finally:
                    with guard:
                        concurrent["active"] -= 1
            return segments(), SimpleNamespace(language="pt")

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeModel))
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: transcript_local(mock_source, "tiny", language="pt"), range(3)))
    assert loaded == ["tiny"]
    assert concurrent["peak"] == 1
    assert all(result["words"][0]["text"] == "fala" for result in results)


def test_failed_inference_cleans_temporary_audio_and_releases_lock(mock_source, monkeypatch):
    paths = []

    class FailingModel:
        def __init__(self, *args, **kwargs):
            pass

        def transcribe(self, path, **kwargs):
            paths.append(Path(path))
            def segments():
                raise RuntimeError("inference failed")
                yield
            return segments(), SimpleNamespace(language="pt")

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FailingModel))
    with pytest.raises(RuntimeError, match="inference failed"):
        transcript_local(mock_source, "tiny")
    assert all(not path.exists() and not path.parent.exists() for path in paths)
    assert not transcription._INFERENCE_LOCK.locked()


def test_no_speech_is_explicit_and_does_not_invent_words(mock_source, monkeypatch):
    class SilentModel:
        def __init__(self, *args, **kwargs):
            pass

        def transcribe(self, path, **kwargs):
            return iter([]), SimpleNamespace(language="en")

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=SilentModel))
    result = transcript_local(mock_source, language="auto")
    assert result["words"] == [] and result["language"] == "und"
    assert "Nenhuma fala" in result["notice"]


def test_source_without_audio_fails_before_model_loading(tmp_path, monkeypatch):
    source = tmp_path / "silent.mp4"
    source.touch()
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout='{"streams":[],"format":{"duration":"5"}}'))
    monkeypatch.setattr(transcription, "_get_model", lambda name: pytest.fail("Must not load a model"))
    with pytest.raises(ValueError, match="faixa de audio"):
        transcript_local(source)


def test_ffmpeg_extracts_only_a_short_mono_16khz_chunk(tmp_path):
    import shutil
    import wave
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg is not installed")
    source, chunk = tmp_path / "source.wav", tmp_path / "chunk.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=48000:duration=3", "-ac", "2", str(source)],
                   check=True, timeout=30)
    assert transcription._audio_duration(source) == pytest.approx(3., abs=.01)
    transcription._extract_chunk(source, chunk, 1., .5)
    with wave.open(str(chunk)) as audio:
        assert audio.getnchannels() == 1 and audio.getframerate() == 16000
        assert audio.getnframes() == pytest.approx(8000, abs=5)
    np = pytest.importorskip("numpy")
    samples = transcription._read_audio_chunk(chunk)
    assert samples.dtype == np.float32 and samples.shape == (8000,)
    assert 0 < np.max(np.abs(samples)) < 1


def test_audio_start_offset_is_preserved_for_synced_captions(tmp_path):
    import array
    import shutil
    import wave
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg is not installed")
    source, chunk = tmp_path / "offset.mp4", tmp_path / "chunk.wav"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
        "color=black:size=160x90:rate=5:duration=4", "-itsoffset", "2", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=1", "-c:v", "libx264", "-c:a", "aac", "-t", "4", str(source),
    ], check=True, timeout=30)
    transcription._extract_chunk(source, chunk, 0., 4.)
    with wave.open(str(chunk)) as audio:
        samples = array.array("h", audio.readframes(audio.getnframes()))
        assert audio.getnframes() == 64000
    assert max(abs(sample) for sample in samples[:16000]) == 0
    assert max(abs(sample) for sample in samples[32000:48000]) > 1000


def test_chunk_overlap_deduplicates_timing_jitter_but_preserves_repeated_words():
    words = [{"start": 179.7, "end": 180.2, "text": "agora"}]
    transcription._merge_chunk_words(words, [
        {"start": 179.8, "end": 180.3, "text": "Agora,"},
        {"start": 180.4, "end": 180.8, "text": "agora"},
    ])
    assert words == [
        {"start": 179.7, "end": 180.3, "text": "agora"},
        {"start": 180.4, "end": 180.8, "text": "agora"},
    ]


def test_enabling_transcription_replaces_an_audio_only_analysis_placeholder(mock_source, monkeypatch):
    import autoclips
    import json
    digest = "a" * 64
    job = autoclips.workspace(mock_source, digest)
    job.mkdir(parents=True)
    (job / "transcript.json").write_text('{"words":[],"engine":"none"}')
    monkeypatch.setattr(autoclips, "probe", lambda source: {"duration": 2., "audio_codec": None})
    monkeypatch.setattr(autoclips, "sha256_file", lambda source: digest)
    monkeypatch.setattr(autoclips, "propose", lambda *a, **kw: [])
    requested = []
    def transcribe(source, model):
        requested.append((source, model))
        return {"words": [{"start": .1, "end": 1., "text": "gravacao"}], "engine": "faster-whisper/tiny"}
    monkeypatch.setattr(autoclips, "transcript_local", transcribe)
    assert autoclips.analyze(mock_source, None, "tiny", clips=1, target=10) == job
    assert requested == [(mock_source, "tiny")]
    assert json.loads((job / "transcript.json").read_text())["words"][0]["text"] == "gravacao"
