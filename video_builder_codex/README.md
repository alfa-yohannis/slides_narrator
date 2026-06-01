# Video Builder Codex

Create a narrated lecture video from a PDF slide deck.

The app generates:

- `slides/slide_*.png`
- `scripts/slide_*.txt`
- `audio/slide_*.mp3`
- `clips/clip_*.mp4`
- `subtitles/clip_*.srt`
- `<final-name>.mp4`
- `<final-name>.srt`

## Requirements

Python dependencies are installed locally into `video_builder_codex/.venv` by default. Use `--no-install` only when you intentionally want to use an existing `edge-tts` executable from your system PATH.

The app also needs these command-line tools:

- `ffmpeg`
- `ffprobe`
- `pdfinfo`
- `pdftoppm`
- `pdftotext`

On Ubuntu/Debian:

```bash
sudo apt-get install ffmpeg poppler-utils
```

## Usage

```bash
python3 video_builder_codex/build.py \
  --pdf slides/pertemuan_13/pertemuan_13.pdf \
  --target videos/pertemuan_13_from_app
```

By default it uses the free Indonesian Edge TTS voice:

```text
id-ID-ArdiNeural
```

By default, slides are rendered at `300` DPI and encoded with the `lossless` video-quality profile. This prioritizes slide clarity and crisp text without wasting much time on oversized intermediate images:

```bash
python3 video_builder_codex/build.py \
  --pdf slides/pertemuan_13/pertemuan_13.pdf \
  --target videos/pertemuan_13_from_app \
  --video-quality lossless
```

Lossless mode uses RGB H.264 (`libx264rgb`, CRF 0), so files are larger and rendering is slower. If you need smaller files or maximum player compatibility, use:

```bash
--video-quality high
```

or:

```bash
--video-quality standard
```

The default output size is `1920x1080`. For sharper 4K output, add:

```bash
--width 3840 --height 2160
```

For unusual PDFs or archival 4K renders, you can also raise the source rasterization:

```bash
--dpi 600
```

By default, narration scripts are generated locally with simple templates. To use Codex CLI with GPT-5.5 and extra-high reasoning for the scripts:

```bash
python3 video_builder_codex/build.py \
  --pdf slides/pertemuan_13/pertemuan_13.pdf \
  --target videos/pertemuan_13_from_app \
  --final-name pertemuan_13 \
  --script-provider codex \
  --codex-model gpt-5.5 \
  --codex-reasoning-effort xhigh
```

This calls:

```text
codex exec --model gpt-5.5 -c model_reasoning_effort="xhigh"
```

Codex script generation uses the extracted slide text and writes `scripts/slide_*.txt`. It still uses `edge-tts` afterward to synthesize audio.

The Codex prompt is tuned to **discuss each content slide in depth** (~70–150 words) instead of just restating bullets: it adds concrete examples/instances, the reasoning behind a concept, everyday analogies, implications/trade-offs, and links to the previous slide, and for code slides it walks through one concrete execution step by step. Title and section-divider slides stay short. The simple `template` provider only summarizes the extracted text and does not produce this depth — use `--script-provider codex` when you want the richer narration.

If Codex reports that `gpt-5.5` requires a newer CLI, upgrade it:

```bash
npm install -g @openai/codex@latest
codex --version
```

Useful options:

```bash
python3 video_builder_codex/build.py \
  --pdf slides/pertemuan_13/pertemuan_13.pdf \
  --target videos/pertemuan_13_from_app \
  --voice id-ID-GadisNeural \
  --rate -5% \
  --final-name pertemuan_13
```

To start from the beginning without manually deleting the target directory, add `--force`:

```bash
python3 video_builder_codex/build.py \
  --pdf slides/pertemuan_13/pertemuan_13.pdf \
  --target videos/pertemuan_13_from_app \
  --final-name pertemuan_13 \
  --script-provider codex \
  --codex-model gpt-5.5 \
  --codex-reasoning-effort xhigh \
  --force
```

`--force` removes generated folders and final files in the target (`slides`, `scripts`, `audio`, `clips`, `subtitles`, `work`, and `<final-name>.mp4/.srt`) before rebuilding.

The app prints progress messages while it works, for example:

```text
[00:00] === Generate Audio ===
[00:12] [03/44] Generating narration audio: slide_03.mp3
[04:31] === Render Clips ===
[04:38] [01/44] Clip ready: clip_01.mp4 (21.5s)
```

Add `--verbose` if you also want to see every external command that is run.

If the online TTS service has a temporary DNS or connection failure, the app retries automatically. To resume a build after a failure, rerun the same command and add `--skip-existing-audio`:

```bash
python3 video_builder_codex/build.py \
  --pdf slides/pertemuan_13/pertemuan_13.pdf \
  --target videos/pertemuan_13_from_app \
  --final-name pertemuan_13 \
  --skip-existing-audio
```

Retry behavior can be adjusted with `--tts-retries`, `--tts-retry-wait`, and `--tts-timeout`.

Audio reuse is script-aware. When `--skip-existing-audio` is used, an existing `slide_*.mp3` is reused only if its sidecar metadata matches the current script text, voice, and rate. If you regenerate scripts with Codex, stale audio is automatically regenerated.

If Codex script generation is interrupted after some scripts are written, rerun with `--skip-existing-scripts`.

For a quick test:

```bash
python3 video_builder_codex/build.py \
  --pdf slides/pertemuan_13/pertemuan_13.pdf \
  --target /tmp/lecture_test \
  --max-pages 2 \
  --final-name sample
```

Install Python dependencies only:

```bash
python3 video_builder_codex/build.py --install-only
```
