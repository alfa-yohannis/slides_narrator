# Slides Narrator

Turn a PDF slide deck into a fully narrated lecture video — with Indonesian
voice‑over and synchronized subtitles — in a single command.

It runs a seven‑stage pipeline that goes from a static PDF to three finished
artifacts — the video (`.mp4`), the subtitles (`.srt`), and ready‑to‑paste
YouTube metadata (`youtube.txt`):

```
PDF ─▶ PNG pages ─▶ narration scripts (Claude | Codex) ─▶ MP3 + SRT (Edge | Gemini TTS)
        ─▶ per‑slide MP4 clips (ffmpeg) ─▶ concatenated MP4 + merged SRT
        ─▶ youtube.txt (title / description / keywords)
```

Each slide gets its own narration script (written in Bahasa Indonesia from the
actual slide content), its own spoken audio track, and its own subtitle file.
Every clip lasts exactly as long as its narration, then all clips and subtitles
are stitched into one video.

The narrator and the TTS engine are each swappable:

- **Narrator** — `--narrator claude` (default, reads the PDF directly) or
  `--narrator codex` (feeds extracted slide text to `codex exec`).
- **TTS** — `--tts-provider edge` (default, free, no key, exact subtitle
  timing) or `--tts-provider gemini` (nicer voices, needs an API key,
  estimated subtitle timing).

---

## Features

- **One command, end to end.** PDF in, narrated video out.
- **Auto‑generated narration** from your slides via Claude Code (default, no API
  key — reuses your existing `claude` session) or the Codex CLI
  (`--narrator codex`).
- **In‑depth narration.** Content slides are explained in depth — concrete
  examples/instances, the reasoning behind each concept, analogies, trade‑offs,
  and a concrete walk‑through for code — not just a re‑read of the bullets.
- **Two TTS engines.** Free Microsoft Edge TTS (`id-ID-ArdiNeural` default;
  `id-ID-GadisNeural` for female) with exact subtitle timing, or Google Gemini
  TTS (`--tts-provider gemini`, default voice `Iapetus`) for nicer voices.
- **Synchronized subtitles** (`.srt`) generated alongside the audio and merged
  with correct global timestamps (exact with Edge, estimated with Gemini).
- **YouTube metadata** (`youtube.txt`) auto‑written from the narration — title,
  description, and keywords, sanitised and clamped to YouTube's length limits.
- **Resumable.** Every stage skips work that already exists, so an interrupted
  run picks up where it left off. Re‑run anytime.
- **Robust TTS** with configurable retries and back‑off for flaky networks.
- **Self‑bootstrapping.** On first run it creates a local `.venv` and installs
  its one Python dependency (`edge-tts`) automatically.
- **Tunable quality** — DPI, resolution, CRF, encoder preset, audio bitrate,
  and parallelism are all flags.
- **Stage selection** — run only the stages you need (`--only`).

---

## Requirements

Everything below is summarised here; the [Installation & setup](#installation--setup)
section has copy‑paste commands.

### System tools (must be on your `PATH`)

| Tool | Provides | Needed for | Install (Debian/Ubuntu) |
|------|----------|------------|--------------------------|
| `ffmpeg`, `ffprobe` | video/audio encoding & probing | **always** | `sudo apt install ffmpeg` |
| `pdftoppm`, `pdfinfo` | PDF → PNG, page count (poppler) | **always** | `sudo apt install poppler-utils` |
| `pdftotext` | slide text extraction | `--narrator codex` | `sudo apt install poppler-utils` |
| `claude` | narration | default narrator | [Claude Code](https://claude.com/claude-code) |
| `codex` | narration | `--narrator codex` | `npm install -g @openai/codex` |

### Python

- **Python 3.9+** (the script uses `from __future__ import annotations`).
- **Runtime dependency:** `edge-tts>=7.0` — listed in
  [`slides_narrator/requirements.txt`](slides_narrator/requirements.txt) and
  installed **automatically** into a local `.venv` on first run. You don't
  install it yourself.
- **Gemini TTS** (`--tts-provider gemini`) needs **no extra Python package** —
  it calls the REST API over the standard library and pipes audio through
  `ffmpeg`.
- **Dev/test dependency:** `pytest` — listed in
  [`slides_narrator/requirements-dev.txt`](slides_narrator/requirements-dev.txt),
  installed only if you want to run the tests.

### Credentials / accounts

| For | What you need | How to set it |
|-----|---------------|---------------|
| Claude narrator (default) | A logged‑in Claude Code session (no API key) | `claude` then `/login`, or `ANTHROPIC_API_KEY` |
| Codex narrator | A logged‑in Codex CLI | `codex login` |
| Gemini TTS | A Gemini API key | `GEMINI_API_KEY` env var or a `.env` at the repo root |

> Edge TTS (the default voice engine) needs **no key and no account**. The only
> things required for a minimal run are `ffmpeg` + poppler + a logged‑in
> `claude` (or pre‑written scripts with `--skip-scripts`).

---

## Installation & setup

### 1. Clone and check system tools

```bash
git clone <repo-url> slides_narrator
cd slides_narrator

# Debian/Ubuntu: the always-required tools
sudo apt update && sudo apt install -y ffmpeg poppler-utils

# verify
ffmpeg -version | head -1
pdftoppm -v 2>&1 | head -1
```

### 2. Python dependencies (automatic)

There is **no manual install step** for the runtime. The first time you run
`build.py` it self‑bootstraps:

1. Creates `slides_narrator/.venv`
2. Installs `edge-tts` (from `requirements.txt`) into it
3. Re‑launches itself inside that venv

```bash
# From the repository root — triggers the one-time bootstrap, then prints help
python3 slides_narrator/build.py --help
```

> If the `.venv` ever breaks (e.g. it was copied from another machine and the
> shebangs point at a missing interpreter), just delete it — it is rebuilt on
> the next run: `rm -rf slides_narrator/.venv`.

### 3. Narrator CLI — pick one

**Claude (default):** install [Claude Code](https://claude.com/claude-code) and
log in once:

```bash
claude          # then run /login, or export ANTHROPIC_API_KEY=sk-ant-...
echo "Reply OK" | claude -p     # verify it answers
```

**Codex (optional, for `--narrator codex`):**

```bash
npm install -g @openai/codex
codex login                      # or configure per Codex docs
codex --version
```

> Don't want either? Pre‑write `…/scripts/slide_NN.txt` yourself and run with
> `--skip-scripts` — no narrator CLI is called.

### 4. Gemini TTS key (optional, for `--tts-provider gemini`)

Get a key from **[Google AI Studio](https://aistudio.google.com/apikey)** (one
key, no billing project needed for the free tier), then make it available in any
**one** of these ways:

```bash
# a) a .env at the repo root (auto-read; keep it gitignored)
echo 'GEMINI_API_KEY=YOUR_KEY_HERE' >> .env

# b) an environment variable
export GEMINI_API_KEY=YOUR_KEY_HERE

# c) pass it inline
python3 slides_narrator/build.py ... --tts-provider gemini --gemini-api-key YOUR_KEY_HERE
```

Resolution order: `--gemini-api-key` → `$GEMINI_API_KEY` → `.env` at the repo
root. The free tier is rate‑limited (≈10 requests/day on the preview TTS model);
enable billing on the API project to lift it. See [pricing](https://ai.google.dev/gemini-api/docs/pricing).

### 5. Dev/test dependencies (optional)

Only needed to run the test suite (see [Testing](#testing)):

```bash
slides_narrator/.venv/bin/pip install -r slides_narrator/requirements-dev.txt
```

---

## Quick start

```bash
python3 slides_narrator/build.py \
  --pdf slides/pertemuan_14/pertemuan_14.pdf \
  --target videos/pertemuan_14 \
  --final-name pertemuan_14
```

This renders the PDF, writes narration scripts, synthesizes audio + subtitles,
encodes per‑slide clips, and produces:

```
videos/pertemuan_14/pertemuan_14.mp4
videos/pertemuan_14/pertemuan_14.srt
```

---

## How it works (the six stages)

| # | Stage | Tool | Output |
|---|-------|------|--------|
| 1 | `pdf` | `pdftoppm` | `slides/slide_NN.png` (one per page) |
| 2 | `scripts` | `claude` CLI (or `codex`) | `scripts/slide_NN.txt` (Indonesian narration) |
| 3 | `audio` | Edge TTS (or Gemini) | `audio/slide_NN.mp3` |
| 5 | `audio` | Edge TTS (or Gemini) | `subtitles/clip_NN.srt` |
| 4 | `clips` | `ffmpeg` | `clips/clip_NN.mp4` (still image + audio) |
| 6 | `merge` | `ffmpeg` | `<final-name>.mp4` + `<final-name>.srt` |
| 7 | `youtube` | `claude` CLI (or `codex`) | `youtube.txt` (title / description / keywords) |

> Stages 3 and 5 happen together. With **Edge TTS** the MP3 and its SRT come
> from a single call, so subtitle timing is exact. With **Gemini TTS** only
> audio is returned, so the SRT is estimated by spreading each script's
> sentences across the clip by length. The numbering follows the conceptual
> pipeline order.

### Output directory layout

For `--target videos/pertemuan_14`:

```
videos/pertemuan_14/
├── slides/            # slide_01.png, slide_02.png, ...
├── scripts/           # slide_01.txt, slide_02.txt, ...   (narration text)
├── audio/             # slide_01.mp3, slide_02.mp3, ...
├── subtitles/         # clip_01.srt, clip_02.srt, ...      (per‑slide subs)
├── clips/             # clip_01.mp4, clip_02.mp4, ...      (per‑slide video)
├── work/              # concat.txt (ffmpeg concat list)
├── pertemuan_14.mp4   # ← final video
├── pertemuan_14.srt   # ← final merged subtitles
└── youtube.txt        # ← YouTube title / description / keywords
```

File numbering is zero‑padded to at least two digits and widened automatically
for decks with 100+ slides.

---

## Resumability

The whole pipeline is **idempotent** — safe to re‑run. Each stage detects work
that is already complete and skips it:

- **Slides** are skipped if `slides/` already has at least as many PNGs as the
  PDF has pages.
- **Scripts** are skipped if `scripts/` already has one `.txt` per page.
- **Audio/subtitles** are skipped per slide when the MP3 (and SRT) already
  exist. Use `--skip-existing-audio` to keep an MP3 even when its SRT is missing.
- **Clips** are skipped per slide if a valid (probe‑able) MP4 already exists;
  corrupt clips are re‑encoded.

To force a full rebuild regardless of existing files, pass `--force`.

If `claude` produces a malformed response, the raw output is saved to
`scripts/_raw_response.txt` for debugging before the run aborts.

---

## Usage examples

### 1. Full pipeline with retry tuning (recommended for flaky networks)

```bash
python3 slides_narrator/build.py \
  --pdf slides/pertemuan_14/pertemuan_14.pdf \
  --target videos/pertemuan_14 \
  --final-name pertemuan_14 \
  --skip-existing-audio \
  --tts-retries 10 \
  --tts-retry-wait 30
```

### 2. Use your own hand‑written narration (no `claude` needed)

Pre‑populate `videos/pertemuan_14/scripts/slide_01.txt`, `slide_02.txt`, … (one
per page), then:

```bash
python3 slides_narrator/build.py \
  --pdf slides/pertemuan_14/pertemuan_14.pdf \
  --target videos/pertemuan_14 \
  --skip-scripts
```

### 3. Female voice, slightly faster, smaller file

```bash
python3 slides_narrator/build.py \
  --pdf slides/pertemuan_03/pertemuan_03.pdf \
  --target videos/pertemuan_03 \
  --voice id-ID-GadisNeural \
  --rate "+10%" \
  --crf 20 \
  --preset medium \
  --audio-bitrate 128k
```

### 4. Codex narrator instead of Claude

Requires the `codex` CLI and `pdftotext`:

```bash
python3 slides_narrator/build.py \
  --pdf slides/pertemuan_14/pertemuan_14.pdf \
  --target videos/pertemuan_14 \
  --narrator codex \
  --codex-model gpt-5.5 \
  --codex-reasoning-effort xhigh
```

### 5. Gemini TTS (voice Iapetus)

Reads `GEMINI_API_KEY` from the environment or the repo‑root `.env`. On the
free tier, keep concurrency low and the retry wait generous:

```bash
python3 slides_narrator/build.py \
  --pdf slides/pertemuan_14/pertemuan_14.pdf \
  --target videos/pertemuan_14 \
  --tts-provider gemini \
  --gemini-voice Iapetus \
  --concurrency 1 \
  --tts-retry-wait 30
```

### 6. Re‑run only specific stages

Re‑encode the clips and re‑merge after tweaking quality settings, without
regenerating scripts or audio:

```bash
python3 slides_narrator/build.py \
  --pdf slides/pertemuan_14/pertemuan_14.pdf \
  --target videos/pertemuan_14 \
  --only clips,merge \
  --force
```

Just (re)generate the narration scripts:

```bash
python3 slides_narrator/build.py \
  --pdf slides/pertemuan_14/pertemuan_14.pdf \
  --target videos/pertemuan_14 \
  --only scripts --force
```

### 7. Force a complete rebuild from scratch

```bash
python3 slides_narrator/build.py \
  --pdf slides/pertemuan_14/pertemuan_14.pdf \
  --target videos/pertemuan_14 \
  --force
```

---

## Recipes — narrator × TTS combinations

The narrator (`--narrator`) and the TTS engine (`--tts-provider`) are
independent, so you can mix and match. The four combinations:

| Narrator | TTS | Command flags | Needs |
|----------|-----|---------------|-------|
| Claude (default) | Edge (default) | *(none — this is the default)* | `claude` |
| Claude | Gemini | `--tts-provider gemini` | `claude`, `GEMINI_API_KEY` |
| Codex | Edge | `--narrator codex` | `codex`, `pdftotext` |
| Codex | Gemini | `--narrator codex --tts-provider gemini` | `codex`, `pdftotext`, `GEMINI_API_KEY` |

### A. Default — Claude narrator + Edge TTS (free, no key)

```bash
python3 slides_narrator/build.py \
  --pdf slides/session02/session02.pdf \
  --target videos/session02
```

### B. Claude narrator + Gemini TTS (nicer voice)

```bash
python3 slides_narrator/build.py \
  --pdf slides/session02/session02.pdf \
  --target videos/session02 \
  --tts-provider gemini --gemini-voice Iapetus \
  --concurrency 1 --tts-retry-wait 30
```

### C. Codex narrator + Edge TTS

```bash
python3 slides_narrator/build.py \
  --pdf slides/session02/session02.pdf \
  --target videos/session02 \
  --narrator codex --codex-model gpt-5.5
```

### D. Codex narrator + Gemini TTS (everything swapped)

```bash
python3 slides_narrator/build.py \
  --pdf slides/session02/session02.pdf \
  --target videos/session02 \
  --narrator codex --codex-model gpt-5.5 \
  --tts-provider gemini --gemini-voice Charon \
  --concurrency 1 --tts-retry-wait 30
```

### E. Generate scripts first, review them, then build the rest

Useful when you want to read/edit the narration before spending time on TTS
and encoding:

```bash
# 1) scripts only (Codex here; drop --narrator for Claude)
python3 slides_narrator/build.py \
  --pdf slides/session02/session02.pdf \
  --target videos/session02 \
  --narrator codex --only scripts

# 2) (optional) edit videos/session02/scripts/slide_*.txt by hand

# 3) audio + clips + merge, reusing your edited scripts
python3 slides_narrator/build.py \
  --pdf slides/session02/session02.pdf \
  --target videos/session02 \
  --skip-scripts --tts-provider gemini
```

### F. Switch TTS engine on an existing build (re‑voice only)

Re‑generate just the audio/subtitles with a different engine or voice, then
re‑encode clips and re‑merge — without touching the scripts:

```bash
python3 slides_narrator/build.py \
  --pdf slides/session02/session02.pdf \
  --target videos/session02 \
  --skip-scripts \
  --tts-provider gemini --gemini-voice Orus \
  --only audio,clips,merge --force
```

### G. Hand‑written scripts + Gemini female‑style voice, 4K

```bash
python3 slides_narrator/build.py \
  --pdf slides/session02/session02.pdf \
  --target videos/session02 \
  --skip-scripts \
  --tts-provider gemini --gemini-voice Iapetus \
  --width 3840 --height 2160 --concurrency 1
```

> **Tip:** on the Gemini **free tier**, always add `--concurrency 1` and a
> larger `--tts-retry-wait` (e.g. `30`) to ride out the per‑minute rate limit
> (HTTP 429). Enabling billing on the API project removes the limit.

---

## Command‑line options

### Required

| Flag | Description |
|------|-------------|
| `--pdf PATH` | Input PDF slide deck. |
| `--target DIR` | Output directory (created if missing). |

### Output naming

| Flag | Default | Description |
|------|---------|-------------|
| `--final-name STEM` | PDF file stem | Base name for the final `.mp4` / `.srt`. |

### Narration

| Flag | Default | Description |
|------|---------|-------------|
| `--narrator` | `claude` | Narration generator: `claude` or `codex`. |
| `--skip-scripts` | off | Use existing `scripts/*.txt`; do not call any narrator. |

**Claude narrator** (default) — reads the PDF directly via its `Read` tool:

| Flag | Default | Description |
|------|---------|-------------|
| `--claude-cmd` | `claude` | Claude CLI executable used for script generation. |
| `--claude-model` | `opus` | Model alias/ID passed to `claude --model`. |
| `--claude-effort` | `high` | Effort level (`low`/`medium`/`high`/`xhigh`/`max`, or `""` to omit). |

**Codex narrator** (`--narrator codex`) — feeds `pdftotext`‑extracted slide
text to `codex exec` with a JSON output schema. Requires the `codex` CLI and
`pdftotext` (poppler‑utils):

| Flag | Default | Description |
|------|---------|-------------|
| `--codex-cmd` | `codex` | Codex CLI executable. |
| `--codex-model` | `gpt-5.5` | Model passed to `codex exec --model`. |
| `--codex-reasoning-effort` | `xhigh` | `model_reasoning_effort` for codex. |
| `--codex-retries` | `2` | Max retries on transient codex failure. |
| `--codex-retry-wait` | `30` | Seconds between codex retries. |
| `--codex-timeout` | `1800` | Seconds before one codex attempt is considered stuck. |

### Text‑to‑speech

| Flag | Default | Description |
|------|---------|-------------|
| `--tts-provider` | `edge` | TTS engine: `edge` or `gemini`. |
| `--skip-existing-audio` | off | Keep existing MP3s even when their SRT is missing. |
| `--tts-retries` | `3` | Max retries per slide on TTS failure. |
| `--tts-retry-wait` | `10.0` | Seconds to wait between TTS retries. |
| `--tts-timeout` | `180` | Per‑request timeout (Gemini only). |

**Edge TTS** (`--tts-provider edge`, default) — free, no key. Emits the MP3 and
**exact** word/sentence subtitle timing together:

| Flag | Default | Description |
|------|---------|-------------|
| `--voice` | `id-ID-ArdiNeural` | Edge TTS voice. Try `id-ID-GadisNeural` for female. |
| `--rate` | `-5%` | Speech rate adjustment (e.g. `-5%`, `+0%`, `+10%`). |

**Gemini TTS** (`--tts-provider gemini`) — nicer voices, needs an API key.
Returns audio only, so subtitle timings are **estimated** (sentences spread
across the clip by length):

| Flag | Default | Description |
|------|---------|-------------|
| `--gemini-voice` | `Iapetus` | Gemini prebuilt voice. Also: `Charon`, `Orus`, `Rasalgethi`, `Algieba`, … |
| `--gemini-tts-model` | `gemini-2.5-flash-preview-tts` | Gemini TTS model. |
| `--gemini-api-key` | — | API key. Falls back to `$GEMINI_API_KEY`, then a `.env` at the repo root. |

> **Free‑tier note:** the Gemini API free tier rate‑limits the preview TTS
> model heavily (a few requests per minute, low daily cap → HTTP 429). For a
> full deck on the free tier, use `--concurrency 1` and a larger
> `--tts-retry-wait`; enabling billing on the API project removes the limit.
> The key in `.env` is read automatically (`GEMINI_API_KEY`).

### Rendering & encoding

| Flag | Default | Description |
|------|---------|-------------|
| `--dpi` | `300` | PDF render DPI. |
| `--width` | `1920` | Output video width. |
| `--height` | `1080` | Output video height. |
| `--crf` | `14` | libx264 CRF; lower = better quality. `0` = mathematically lossless, `~14` ≈ visually lossless. |
| `--preset` | `slow` | libx264 preset (`ultrafast` … `veryslow`); slower = better compression. |
| `--audio-bitrate` | `256k` | AAC audio bitrate. |
| `--concurrency` | `6` | Parallel TTS / ffmpeg workers. |

### Flow control

| Flag | Default | Description |
|------|---------|-------------|
| `--only` | (all) | Comma‑separated stages to run: `pdf,scripts,audio,clips,merge,youtube`. |
| `--skip-youtube` | off | Do not generate `youtube.txt`. |
| `--force` | off | Regenerate outputs even if they already exist. |

Run `python3 slides_narrator/build.py --help` to see all options with the
embedded pipeline documentation.

---

## Narration generation details

In stage 2 the tool invokes the `claude` CLI with the PDF as input and a
carefully constructed Indonesian prompt. Claude reads the PDF directly (using
its `Read` tool, granted via `--allowedTools Read --add-dir <pdf dir>`) and
returns a single JSON object containing exactly one narration string per page.
The prompt instructs the model to:

- Write flowing spoken narration (not bullet points).
- Keep title/section‑break slides to 1–2 sentences.
- **Discuss content slides in depth** (~6–12 sentences) rather than restating
  the bullets — using concrete examples/instances, the reasoning behind the
  concept, everyday analogies, implications/trade‑offs, and links back to the
  previous slide, picking whichever techniques fit the material.
- Explain code slides by purpose and key logic, not line‑by‑line, then walk
  through one concrete execution (a specific input/object, step by step).
- Avoid naming slide numbers and spell out numbers/symbols in Indonesian.

Each returned string is written to `scripts/slide_NN.txt`. You can freely edit
these files afterward and re‑run with `--only audio,clips,merge` (or
`--skip-scripts`) to regenerate the video from your edits.

The run streams Claude's progress live (session start, assistant text, tool
calls, results) and prints a heartbeat while waiting. There is a 30‑minute hard
cap on the script‑generation step.

### Codex narrator (`--narrator codex`)

Codex can't read the PDF binary, so this path extracts each page's text with
`pdftotext` first, then sends all pages to `codex exec` in one call with a JSON
**output schema** (`--output-schema`) so the response is one narration string
per slide. It uses the same in‑depth narration guidance as the Claude prompt.
Requires the `codex` CLI (`npm install -g @openai/codex`) and `pdftotext`. On a
transient failure it retries (`--codex-retries`, `--codex-retry-wait`); if Codex
reports the model needs a newer CLI, upgrade with `npm install -g @openai/codex@latest`.

---

## Troubleshooting

- **`Required system tool not found: <name>`** — install the missing tool (see
  [Requirements](#requirements)).
- **`claude CLI not found in PATH`** — install Claude Code, or supply your own
  scripts and pass `--skip-scripts`.
- **`codex CLI not found in PATH`** (with `--narrator codex`) — install/login to
  Codex (`npm install -g @openai/codex`), or pass `--skip-scripts`.
- **`Expected N scripts, got M`** — the narrator returned the wrong number of
  entries; for Claude, inspect `scripts/_raw_response.txt`, then re‑run
  `--only scripts --force`.
- **`edge-tts produced 0-byte audio` / TTS retries exhausted** — usually a
  transient network issue. Increase `--tts-retries` / `--tts-retry-wait`, lower
  `--concurrency`, and re‑run (completed slides are skipped).
- **`gemini HTTP 429` / quota exceeded** (with `--tts-provider gemini`) — the
  free tier rate‑limits the preview TTS model. Use `--concurrency 1` and a
  larger `--tts-retry-wait` (e.g. `30`), or enable billing on the API project.
- **`gemini TTS requires an API key`** — set `GEMINI_API_KEY` (environment or a
  `.env` at the repo root), or pass `--gemini-api-key`.
- **A clip looks corrupt** — re‑run with `--only clips --force` to re‑encode.
- **Wrong number of slides rendered** — delete the `slides/` folder and re‑run
  the `pdf` stage, or use `--force`.

---

## Testing

The suite lives in [`tests/`](tests/) and is split into **unit** tests (pure
functions — timestamps, SRT parsing/estimation, the codex prompt/schema, Gemini
key resolution and HTTP‑response parsing with the network mocked) and
**integration** tests (real `ffmpeg`/poppler stages driven on a hand‑built PDF;
the Gemini path is exercised with synthesis monkey‑patched to silent PCM, so
**no network or API key is used**).

Install the test dependency once, then run from the repo root:

```bash
# one-time
slides_narrator/.venv/bin/pip install -r slides_narrator/requirements-dev.txt

# run everything
slides_narrator/.venv/bin/python -m pytest

# (or, if pytest is on your PATH and edge-tts is importable)
pytest
```

Run a subset:

```bash
# only unit tests (fast, no external tools)
pytest -m "not integration"

# only integration tests (need ffmpeg + poppler; auto-skipped if missing)
pytest -m integration

# a single file
pytest tests/test_unit.py

# a single test by name
pytest tests/test_unit.py::test_fmt_ts_basic

# anything matching a keyword, verbose
pytest -k gemini -v
```

Notes:

- Integration tests **auto‑skip** when a required binary (`ffmpeg`, `ffprobe`,
  `pdftoppm`, `pdfinfo`, `pdftotext`) is absent, so the unit tests always run
  anywhere.
- No test hits the live Edge or Gemini services or spends Gemini quota.
- The tests import `build.py` directly (the venv bootstrap is skipped via the
  `SLIDES_NARRATOR_VENV` marker and `edge_tts` is stubbed if absent), so they
  run under any Python 3.9+ with `pytest` available — the app's `.venv` is the
  simplest place to get that.

---

## Publishing to YouTube (`youtube.txt`)

The final **`youtube`** stage writes a `youtube.txt` next to the `.mp4` / `.srt`,
holding ready‑to‑paste metadata for YouTube Studio. It is produced automatically
on a normal run (the same selected `--narrator` — Claude or Codex — writes it
from the narration transcript, in the narration's language). Three sections under
plain headers:

```
TITLE
<one line>

DESCRIPTION
<free text — no emoticons; ends with relevant #hashtags>

KEYWORDS
<comma-separated tags (no #)>
```

Each field is sanitised and **clamped to YouTube's 2026 limits** before writing,
so the output is always within bounds:

| Field | Hard limit | What the stage does |
|-------|-----------|---------------------|
| Title | **100 characters** | front‑loads key terms in the first ~70; strips emoji and hashtags; clamps to 100 at a word boundary |
| Description | **5,000 characters** | hook in the first ~157, then an overview + bullet list, ending with relevant `#hashtags`; strips emoji (keeps hashtags), caps at **15 hashtags** (YouTube ignores them all past 15); clamps to 5,000 |
| Keywords (tags) | **500 characters combined** | plain comma‑separated tags (no `#`); drops trailing tags so the total stays ≤ 500 |

Control it:

```bash
# default: generated as part of the full run, no extra flags needed

# regenerate only youtube.txt for an existing build (reuses the scripts)
python3 slides_narrator/build.py --pdf deck.pdf --target out \
  --skip-scripts --only youtube --force

# turn it off
python3 slides_narrator/build.py --pdf deck.pdf --target out --skip-youtube
```

Notes:

- It needs the selected narrator CLI (`claude` by default, or `codex`). If that
  CLI is missing, the stage **logs a warning and skips** — the video and
  subtitles are never put at risk by it.
- With `--tts-provider gemini` and no narrator CLI installed, pass
  `--skip-youtube` (or install a narrator) to avoid the skip notice.

(Re)verify the lengths of any `youtube.txt` at a glance:

```bash
python3 - <<'PY'
import re
t = open("youtube.txt", encoding="utf-8").read()
sec = lambda a,b: (re.search(rf"^{a}\n(.*?)(?:\n{b}\n|\Z)", t, re.S|re.M) or [None,""])[1].strip()
title, desc = sec("TITLE","DESCRIPTION"), sec("DESCRIPTION","KEYWORDS")
kw = sec("KEYWORDS","ZZZ")
print(f"title       {len(title):>5}/100   {'OK' if len(title)<=100 else 'OVER'}")
print(f"description {len(desc):>5}/5000  {'OK' if len(desc)<=5000 else 'OVER'}")
print(f"keywords    {len(kw):>5}/500   {'OK' if len(kw)<=500 else 'OVER'}")
PY
```

---

## Notes

- The `.venv/` and `__pycache__/` directories inside `slides_narrator/` are
  generated artifacts and can be safely deleted; they will be recreated.
- Subtitles are merged with cumulative offsets so timestamps stay correct across
  the full concatenated video, and each cue is clamped to its clip's duration.
- Slides are scaled to fit the target resolution with white padding (letterbox),
  preserving aspect ratio.
