"""Unit tests for pure helpers in slides_narrator/build.py.

These exercise the timestamp math, subtitle parsing/estimation, the codex
prompt/schema/response plumbing, the Gemini key resolution, and the Gemini HTTP
response parsing (with urlopen mocked). No external tools or network needed.
"""

from __future__ import annotations

import io
import json
import re
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


# --- YouTube metadata helpers ----------------------------------------------


def test_strip_emoji_hashtags_removes_emoji_and_tags(b):
    # title path: strips both emoji AND hashtags
    out = b._strip_emoji_hashtags("Belajar ArchiMate 🚀 #keren #ea sekarang")
    assert "🚀" not in out
    assert "#keren" not in out and "#ea" not in out
    assert "Belajar ArchiMate" in out and "sekarang" in out


def test_strip_emoji_hashtags_keeps_punctuation_and_csharp(b):
    # em dash, ellipsis, curly quotes and "C#" must survive
    src = "Pemrograman C# — lanjutan… selesai"
    out = b._strip_emoji_hashtags(src)
    assert "C#" in out
    assert "—" in out and "…" in out


def test_strip_emoji_keeps_hashtags(b):
    # description path: strips emoji but KEEPS hashtags
    out = b._strip_emoji("Materi keren 🚀\n#ArchiMate #EnterpriseArchitecture")
    assert "🚀" not in out
    assert "#ArchiMate" in out and "#EnterpriseArchitecture" in out


def test_cap_hashtags_limits_to_15(b):
    tags = " ".join(f"#tag{i}" for i in range(20))
    out = b._cap_hashtags("Deskripsi. " + tags, max_tags=15)
    kept = re.findall(r"#\w+", out)
    assert len(kept) == 15
    assert kept[0] == "#tag0" and kept[-1] == "#tag14"


def test_cap_hashtags_keeps_all_when_under_limit(b):
    src = "Deskripsi. #a #b #c"
    assert b._cap_hashtags(src) == src


def test_clamp_word_boundary(b):
    text = "satu dua tiga empat lima enam tujuh"
    out = b._clamp(text, 12)
    assert len(out) <= 12
    assert not out.endswith(" ")
    # should not cut mid-word when a boundary is near
    assert out in ("satu dua", "satu dua tiga"[:12].rstrip())


def test_clamp_keywords_total_and_drop(b):
    kw = ", ".join([f"tag{i:02d}" for i in range(200)])  # way over 500
    out = b._clamp_keywords(kw, 500)
    assert len(out) <= 500
    # all kept tags are intact (no half-tag at the end)
    assert all(t.strip().startswith("tag") for t in out.split(","))


def test_clamp_keywords_handles_newlines(b):
    out = b._clamp_keywords("a,\nb , c\n", 500)
    assert out == "a, b, c"


def test_extract_json_variants(b):
    assert b._extract_json('{"title": "x"}')["title"] == "x"
    assert b._extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert b._extract_json('blah {"a": 2} blah') == {"a": 2}


def test_youtube_schema_maxlengths(b):
    sch = b._youtube_schema()["properties"]
    assert sch["title"]["maxLength"] == 100
    assert sch["description"]["maxLength"] == 5000
    assert sch["keywords"]["maxLength"] == 500


def test_youtube_text_layout(b):
    txt = b._youtube_text("T", "D", "k1, k2")
    assert txt.startswith("TITLE\nT\n\nDESCRIPTION\nD\n\nKEYWORDS\nk1, k2\n")


def test_youtube_prompt_includes_limits_and_transcript(b):
    p = b._youtube_prompt("Halo dunia.")
    assert "100 characters" in p and "5000 characters" in p and "500" in p
    assert "Halo dunia." in p
    # description must ask for hashtags; title must forbid them
    assert "hashtags" in p
    assert "#EnterpriseArchitecture" in p


def test_stage_generate_youtube_clamps_and_writes(b, tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    scripts.joinpath("slide_01.txt").write_text("Materi satu.", encoding="utf-8")
    scripts.joinpath("slide_02.txt").write_text("Materi dua.", encoding="utf-8")

    # Narrator returns over-limit + emoji content; title has a hashtag (must be
    # stripped), description has hashtags (must be KEPT) and emoji (stripped).
    fake = {
        "title": "T" * 130 + " 🚀 #nope",
        "description": "Hook penting. 🚀 " + ("x " * 1500)
                       + "\n#ArchiMate #EnterpriseArchitecture",
        "keywords": ", ".join(f"kw{i:03d}" for i in range(300)),
    }
    monkeypatch.setattr(b, "_youtube_via_claude", lambda *a, **k: fake)
    monkeypatch.setattr(b.shutil, "which", lambda name: "/usr/bin/" + name)

    out = tmp_path / "youtube.txt"
    b.stage_generate_youtube(
        scripts, out, tmp_path / "work", page_count=2, narrator="claude",
        claude_cmd="claude", claude_model="opus", claude_effort="high",
        codex_cmd="codex", codex_model="gpt-5.5", codex_effort="xhigh",
        codex_timeout=60.0, force=False,
    )
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    title = content.split("TITLE\n", 1)[1].split("\n\n", 1)[0]
    desc = content.split("DESCRIPTION\n", 1)[1].split("\n\nKEYWORDS", 1)[0]
    kw = content.split("KEYWORDS\n", 1)[1].strip()
    assert len(title) <= 100 and "🚀" not in title and "#nope" not in title
    assert len(desc) <= 5000 and "🚀" not in desc
    # hashtags preserved in the description
    assert "#ArchiMate" in desc and "#EnterpriseArchitecture" in desc
    assert len(kw) <= 500 and "#" not in kw


def test_stage_generate_youtube_skips_when_cli_missing(b, tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    scripts.joinpath("slide_01.txt").write_text("Materi.", encoding="utf-8")
    monkeypatch.setattr(b.shutil, "which", lambda name: None)  # no narrator CLI
    out = tmp_path / "youtube.txt"
    # must not raise, and must not write the file
    b.stage_generate_youtube(
        scripts, out, tmp_path / "work", page_count=1, narrator="claude",
        claude_cmd="claude", claude_model="opus", claude_effort="high",
        codex_cmd="codex", codex_model="gpt-5.5", codex_effort="xhigh",
        codex_timeout=60.0, force=False,
    )
    assert not out.exists()
