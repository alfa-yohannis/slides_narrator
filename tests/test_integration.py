"""Integration tests that drive real pipeline stages end to end.

They use a hand-built PDF plus ffmpeg/poppler — no network and no API keys. A
test is skipped automatically when a required binary is missing. The Gemini path
is exercised with `_gemini_synth` monkeypatched to return real (silent) PCM, so
the PCM->MP3 + estimated-SRT plumbing is covered without spending quota.
"""

from __future__ import annotations

import pytest

from conftest import make_test_pdf, requires, silent_mp3, pcm_silence


pytestmark = pytest.mark.integration


@requires("pdfinfo")
def test_get_page_count(b, tmp_path):
    pdf = make_test_pdf(tmp_path / "deck.pdf", ["Slide One", "Slide Two", "Slide Three"])
    assert b.get_page_count(pdf) == 3


@requires("pdftoppm", "pdfinfo")
def test_stage_pdf_to_pngs(b, tmp_path):
    pdf = make_test_pdf(tmp_path / "deck.pdf", ["A", "B"])
    slides_dir = tmp_path / "slides"
    n = b.stage_pdf_to_pngs(pdf, slides_dir, dpi=72)
    assert n == 2
    assert (slides_dir / "slide_01.png").exists()
    assert (slides_dir / "slide_02.png").exists()


@requires("pdftoppm", "pdfinfo")
def test_stage_pdf_to_pngs_idempotent(b, tmp_path):
    pdf = make_test_pdf(tmp_path / "deck.pdf", ["A", "B"])
    slides_dir = tmp_path / "slides"
    b.stage_pdf_to_pngs(pdf, slides_dir, dpi=72)
    mtimes = {p.name: p.stat().st_mtime_ns for p in slides_dir.glob("slide_*.png")}
    # second call should skip rendering (files unchanged)
    b.stage_pdf_to_pngs(pdf, slides_dir, dpi=72)
    again = {p.name: p.stat().st_mtime_ns for p in slides_dir.glob("slide_*.png")}
    assert mtimes == again


@requires("pdftotext")
def test_extract_page_text(b, tmp_path):
    pdf = make_test_pdf(tmp_path / "deck.pdf", ["Hello Architecture", "Second Page"])
    lines = b._extract_page_text(pdf, 1, 2, "pdftotext")
    joined = " ".join(lines)
    assert "Hello" in joined and "Architecture" in joined


@requires("ffmpeg", "ffprobe", "pdftoppm", "pdfinfo")
def test_clips_and_merge_pipeline(b, tmp_path):
    pdf = make_test_pdf(tmp_path / "deck.pdf", ["One", "Two"])
    slides = tmp_path / "slides"
    audio = tmp_path / "audio"
    clips = tmp_path / "clips"
    subs = tmp_path / "subtitles"
    work = tmp_path / "work"
    for d in (audio, subs):
        d.mkdir()

    n = b.stage_pdf_to_pngs(pdf, slides, dpi=72)
    # fabricate per-slide audio + subtitles
    for i in range(1, n + 1):
        silent_mp3(audio / f"slide_{i:02d}.mp3", seconds=1.0)
        b._write_estimated_srt(f"Kalimat {i}.", 1.0, subs / f"clip_{i:02d}.srt")

    b.stage_clips(
        slides, audio, clips, n,
        width_px=320, height_px=180, concurrency=2, force=False,
        crf=30, preset="ultrafast", audio_bitrate="64k",
    )
    assert (clips / "clip_01.mp4").exists()
    assert (clips / "clip_02.mp4").exists()

    out_mp4 = tmp_path / "final.mp4"
    out_srt = tmp_path / "final.srt"
    b.stage_merge(clips, subs, work, out_mp4, out_srt, n)

    assert out_mp4.exists() and out_mp4.stat().st_size > 0
    assert b._ffprobe_duration(out_mp4) > 1.5  # two ~1s clips concatenated
    # merged SRT cues are offset across clips and strictly increasing
    cues = b._parse_srt(out_srt)
    assert len(cues) == 2
    assert cues[1][0] >= cues[0][1]


@requires("ffmpeg", "ffprobe")
def test_gemini_tts_one_pcm_to_mp3(b, tmp_path, monkeypatch):
    # Avoid the network: return 1s of real silent PCM at 24 kHz.
    monkeypatch.setattr(b, "_gemini_synth", lambda *a, **k: (pcm_silence(1.0, 24000), 24000))

    mp3 = tmp_path / "slide_01.mp3"
    srt = tmp_path / "clip_01.srt"
    b._gemini_tts_one("Kalimat satu. Kalimat dua.", mp3, srt, "key",
                      "gemini-2.5-flash-preview-tts", "Iapetus", 30.0)

    assert mp3.exists() and mp3.stat().st_size > 0
    assert b._ffprobe_duration(mp3) == pytest.approx(1.0, abs=0.3)
    cues = b._parse_srt(srt)
    assert len(cues) == 2
    assert cues[-1][1] == pytest.approx(b._ffprobe_duration(mp3), abs=0.05)


@requires("ffmpeg", "ffprobe")
def test_stage_tts_gemini_skips_when_present(b, tmp_path, monkeypatch):
    """stage_tts(provider=gemini) should no-op when mp3+srt already exist."""
    scripts = tmp_path / "scripts"
    audio = tmp_path / "audio"
    subs = tmp_path / "subtitles"
    for d in (scripts, audio, subs):
        d.mkdir()
    scripts.joinpath("slide_01.txt").write_text("Halo.", encoding="utf-8")
    audio.joinpath("slide_01.mp3").write_bytes(b"x")
    subs.joinpath("clip_01.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHalo\n", encoding="utf-8")

    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("should not synthesize when outputs exist")

    monkeypatch.setattr(b, "_gemini_synth", boom)
    b.stage_tts(
        scripts, audio, subs, page_count=1, provider="gemini",
        voice="x", rate="-5%", gemini_model="m", gemini_voice="Iapetus",
        gemini_api_key="key", concurrency=1, force=False,
        skip_existing_audio=False, retries=0, retry_wait=0.0, tts_timeout=5.0,
    )
    assert called["n"] == 0


def test_stage_tts_gemini_requires_key(b, tmp_path):
    scripts = tmp_path / "scripts"
    audio = tmp_path / "audio"
    subs = tmp_path / "subtitles"
    for d in (scripts, audio, subs):
        d.mkdir()
    scripts.joinpath("slide_01.txt").write_text("Halo.", encoding="utf-8")

    with pytest.raises(SystemExit) as ei:
        b.stage_tts(
            scripts, audio, subs, page_count=1, provider="gemini",
            voice="x", rate="-5%", gemini_model="m", gemini_voice="Iapetus",
            gemini_api_key=None, concurrency=1, force=False,
            skip_existing_audio=False, retries=0, retry_wait=0.0, tts_timeout=5.0,
        )
    assert "requires an API key" in str(ei.value)
