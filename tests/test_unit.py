"""Unit tests for pure helpers in slides_narrator/build.py.

These exercise the timestamp math, subtitle parsing/estimation, the codex
prompt/schema/response plumbing, the Gemini key resolution, and the Gemini HTTP
response parsing (with urlopen mocked). No external tools or network needed.
"""

from __future__ import annotations

import io
import json
import subprocess

import pytest


# --- timestamps ------------------------------------------------------------


def test_fmt_ts_basic(b):
    assert b._fmt_ts(0) == "00:00:00,000"
    assert b._fmt_ts(1.25) == "00:00:01,250"
    assert b._fmt_ts(3661.5) == "01:01:01,500"


def test_fmt_ts_clamps_negative(b):
    assert b._fmt_ts(-5) == "00:00:00,000"


def test_fmt_ts_ms_rounding_carry(b):
    # 0.9999 s rounds to 1000 ms and must carry into seconds, not print ",1000".
    assert b._fmt_ts(0.9999) == "00:00:01,000"


def test_parse_ts(b):
    assert b._parse_ts("00:00:01,250") == pytest.approx(1.25)
    assert b._parse_ts("01:01:01,500") == pytest.approx(3661.5)


@pytest.mark.parametrize("t", [0.0, 0.001, 1.5, 59.999, 3661.123])
def test_ts_roundtrip(b, t):
    assert b._parse_ts(b._fmt_ts(t)) == pytest.approx(t, abs=0.001)


def test_parse_ts_bad_raises(b):
    with pytest.raises(ValueError):
        b._parse_ts("not-a-timestamp")


# --- sentence splitting & estimated SRT ------------------------------------


def test_split_sentences(b):
    assert b._split_sentences("Halo dunia. Foo bar! Baz?") == [
        "Halo dunia.",
        "Foo bar!",
        "Baz?",
    ]


def test_split_sentences_empty(b):
    assert b._split_sentences("   ") == []


def test_write_estimated_srt(b, tmp_path):
    srt = tmp_path / "clip.srt"
    text = "Kalimat satu. Kalimat dua."
    b._write_estimated_srt(text, 10.0, srt)

    cues = b._parse_srt(srt)
    assert len(cues) == 2
    # first starts at 0, last ends exactly at the clip duration
    assert cues[0][0] == pytest.approx(0.0)
    assert cues[-1][1] == pytest.approx(10.0)
    # monotonic, non-overlapping
    assert cues[0][1] <= cues[1][0] + 1e-6
    # split is proportional to sentence length
    s1, s2 = "Kalimat satu.", "Kalimat dua."
    expected_boundary = 10.0 * len(s1) / (len(s1) + len(s2))
    assert cues[0][1] == pytest.approx(expected_boundary, abs=0.01)
    assert cues[0][2] == s1


def test_write_estimated_srt_empty_text(b, tmp_path):
    srt = tmp_path / "clip.srt"
    b._write_estimated_srt("   ", 5.0, srt)
    assert srt.read_text() == ""


# --- SRT parsing / merging round-trips -------------------------------------


def test_parse_srt_roundtrip(b, tmp_path):
    content = (
        "1\n00:00:00,000 --> 00:00:02,000\nHello\n\n"
        "2\n00:00:02,000 --> 00:00:04,500\nWorld line two\n"
    )
    srt = tmp_path / "x.srt"
    srt.write_text(content, encoding="utf-8")
    cues = b._parse_srt(srt)
    assert cues == [
        (0.0, 2.0, "Hello"),
        (2.0, 4.5, "World line two"),
    ]


def test_parse_srt_missing_file(b, tmp_path):
    assert b._parse_srt(tmp_path / "nope.srt") == []


# --- codex schema / prompt / response --------------------------------------


def test_codex_schema_bounds(b):
    schema = b._codex_schema(3)
    arr = schema["properties"]["scripts"]
    assert arr["minItems"] == 3 and arr["maxItems"] == 3
    assert arr["items"]["properties"]["slide"]["maximum"] == 3
    assert schema["additionalProperties"] is False


def test_codex_prompt_contains_slides_and_depth_rules(b):
    slides = [{"slide": 1, "text": "Hello"}, {"slide": 2, "text": ""}]
    prompt = b._codex_prompt(slides)
    assert '<slide number="01">' in prompt
    assert '<slide number="02">' in prompt
    assert "Hello" in prompt
    # empty text gets the Indonesian placeholder
    assert "Tidak ada teks" in prompt
    # the in-depth narration guidance is present
    assert "DEPTH" in prompt
    assert "EXAMPLE" in prompt


def test_load_codex_response_plain(b, tmp_path):
    p = tmp_path / "r.json"
    p.write_text('{"scripts": [{"slide": 1, "text": "hi"}]}', encoding="utf-8")
    data = b._load_codex_response(p)
    assert data["scripts"][0]["slide"] == 1


def test_load_codex_response_code_fenced(b, tmp_path):
    p = tmp_path / "r.json"
    p.write_text('```json\n{"scripts": []}\n```', encoding="utf-8")
    assert b._load_codex_response(p) == {"scripts": []}


def test_load_codex_response_embedded(b, tmp_path):
    p = tmp_path / "r.json"
    p.write_text('here you go: {"scripts": [1]} thanks', encoding="utf-8")
    assert b._load_codex_response(p) == {"scripts": [1]}


def test_codex_error_details_extracts_message(b):
    exc = subprocess.CalledProcessError(
        1, ["codex"], output='{"error": {"message": "boom happened"}}', stderr=""
    )
    details = b._codex_error_details(exc)
    assert "boom happened" in details


# --- Gemini key resolution -------------------------------------------------


def test_resolve_gemini_key_cli_wins(b, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    assert b._resolve_gemini_key("from-cli") == "from-cli"


def test_resolve_gemini_key_env(b, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    assert b._resolve_gemini_key(None) == "from-env"


def test_resolve_gemini_key_from_dotenv(b, monkeypatch, tmp_path):
    # Point APP_DIR at a temp app dir so APP_DIR.parent/.env is isolated.
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (tmp_path / ".env").write_text(
        '# comment\nGEMINI_API_KEY="dotenv-secret"\nOTHER=1\n', encoding="utf-8"
    )
    monkeypatch.setattr(b, "APP_DIR", app_dir)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert b._resolve_gemini_key(None) == "dotenv-secret"


def test_resolve_gemini_key_absent(b, monkeypatch, tmp_path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    monkeypatch.setattr(b, "APP_DIR", app_dir)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert b._resolve_gemini_key(None) is None


# --- Gemini HTTP response parsing (mocked) ---------------------------------


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_gemini_synth_parses_audio(b, monkeypatch):
    import base64

    pcm = b"\x01\x02\x03\x04"
    payload = {
        "candidates": [
            {"content": {"parts": [
                {"inlineData": {
                    "mimeType": "audio/L16;codec=pcm;rate=24000",
                    "data": base64.b64encode(pcm).decode(),
                }}
            ]}}
        ]
    }

    def fake_urlopen(req, timeout=None):
        return _FakeResp(json.dumps(payload).encode())

    monkeypatch.setattr(b.urllib.request, "urlopen", fake_urlopen)
    out_pcm, rate = b._gemini_synth("hi", "key", "model", "Iapetus", 30.0)
    assert out_pcm == pcm
    assert rate == 24000


def test_gemini_synth_http_error_becomes_runtimeerror(b, monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise b.urllib.error.HTTPError(
            "url", 429, "Too Many Requests", {}, io.BytesIO(b'{"error":"quota"}')
        )

    monkeypatch.setattr(b.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError) as ei:
        b._gemini_synth("hi", "key", "model", "Iapetus", 30.0)
    assert "429" in str(ei.value)


def test_gemini_synth_no_audio_raises(b, monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _FakeResp(json.dumps({"candidates": []}).encode())

    monkeypatch.setattr(b.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError):
        b._gemini_synth("hi", "key", "model", "Iapetus", 30.0)
