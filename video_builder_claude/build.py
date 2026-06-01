#!/usr/bin/env python3
"""End-to-end lecture video builder.

Pipeline:
  1. PDF -> per-page PNGs (pdftoppm)
  2. Per-slide Indonesian narration scripts via the `claude` CLI (default) or
     the `codex` CLI (--narrator codex).
     - `claude`: reads the PDF directly via its Read tool; no API key required.
     - `codex`: feeds pdftotext-extracted slide text to `codex exec` with a
       JSON output schema. Requires the `codex` CLI and `pdftotext` (poppler).
     - If <target>/scripts/slide_NN.txt already exist they are used as-is.
  3. Per-slide MP3 narration via Microsoft Edge TTS (free, id-ID-ArdiNeural)
     - Failures are retried up to --tts-retries with --tts-retry-wait between attempts.
  4. Per-slide MP4 clips (ffmpeg)
  5. Per-clip SRT subtitles (emitted by edge-tts alongside the MP3)
  6. Concatenated final MP4 + merged SRT

Example:
  python3 video_builder_claude/build.py \\
    --pdf slides/pertemuan_14/pertemuan_14.pdf \\
    --target videos/pertemuan_14 \\
    --final-name pertemuan_14 \\
    --skip-existing-audio \\
    --tts-retries 10 \\
    --tts-retry-wait 30

System requirements: ffmpeg, ffprobe, pdftoppm, pdfinfo (poppler-utils),
and the `claude` CLI (Claude Code) for stage 2.
Python deps (auto-installed into local .venv): edge-tts.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: relaunch inside a local venv with deps installed.
# ---------------------------------------------------------------------------

APP_DIR = Path(__file__).resolve().parent
VENV_DIR = APP_DIR / ".venv"
VENV_MARK = "VIDEO_BUILDER_CLAUDE_VENV"


def _bootstrap_venv() -> None:
    if os.environ.get(VENV_MARK) == "1":
        return
    if not VENV_DIR.exists():
        print(f"[setup] Creating virtual environment at {VENV_DIR}")
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])
    pip = VENV_DIR / "bin" / "pip"
    python = VENV_DIR / "bin" / "python"
    if not pip.exists():
        sys.exit(f"[setup] venv looks broken; missing {pip}")
    req = APP_DIR / "requirements.txt"
    print("[setup] Installing dependencies (edge-tts)...")
    subprocess.check_call([str(pip), "install", "-q", "--upgrade", "pip"])
    subprocess.check_call([str(pip), "install", "-q", "-r", str(req)])
    env = os.environ.copy()
    env[VENV_MARK] = "1"
    os.execvpe(str(python), [str(python), str(Path(__file__).resolve()), *sys.argv[1:]], env)


_bootstrap_venv()

# After bootstrap we are running inside the venv.
import edge_tts  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_START = time.monotonic()


def _elapsed() -> str:
    el = int(time.monotonic() - _START)
    return f"{el // 60:02d}:{el % 60:02d}"


def log(msg: str) -> None:
    """Print a progress line with an elapsed-time prefix, flushed immediately."""
    print(f"[{_elapsed()}] {msg}", flush=True)


def need_bin(name: str) -> None:
    if shutil.which(name) is None:
        sys.exit(f"[fatal] Required system tool not found: {name}")


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def get_page_count(pdf: Path) -> int:
    out = subprocess.check_output(["pdfinfo", str(pdf)]).decode()
    for line in out.splitlines():
        if line.startswith("Pages:"):
            return int(line.split(":", 1)[1].strip())
    sys.exit("[fatal] could not read page count from PDF")


# ---------------------------------------------------------------------------
# Stage 1: PDF -> PNGs
# ---------------------------------------------------------------------------


def stage_pdf_to_pngs(pdf: Path, slides_dir: Path, dpi: int) -> int:
    need_bin("pdftoppm")
    slides_dir.mkdir(parents=True, exist_ok=True)

    page_count = get_page_count(pdf)
    existing = sorted(slides_dir.glob("slide_*.png"))
    if len(existing) >= page_count:
        log(f"[1/6] slides/ already populated ({len(existing)} PNGs) — skipping render")
        return page_count

    log(f"[1/6] Rendering {page_count} pages -> {slides_dir} at {dpi} DPI")
    prefix = slides_dir / "slide"
    run([
        "pdftoppm",
        "-png",
        "-r", str(dpi),
        str(pdf),
        str(prefix),
    ])
    # Normalize pdftoppm's "slide-N.png" / "slide-NN.png" outputs to a uniform
    # "slide_NN.png" with at least 2-digit zero padding, matching what the
    # rest of the pipeline expects.
    width = max(2, len(str(page_count)))
    for f in slides_dir.glob("slide-*.png"):
        m = re.match(r"slide-(\d+)\.png$", f.name)
        if not m:
            continue
        n = int(m.group(1))
        target = slides_dir / f"slide_{n:0{width}d}.png"
        if target != f:
            f.rename(target)
    log(f"[1/6] Rendered {page_count} PNGs")
    return page_count


# ---------------------------------------------------------------------------
# Stage 2: Generate narration scripts via the `claude` CLI
# ---------------------------------------------------------------------------


SCRIPT_PROMPT_TEMPLATE = """Tugas: hasilkan skrip narasi lisan dalam Bahasa Indonesia
untuk setiap halaman dari sebuah slide deck PDF.

PDF target (baca dengan tool Read; dukung PDF biner):
  {pdf}

PDF tersebut memiliki tepat {n} halaman. Hasilkan tepat {n} entri skrip,
satu untuk setiap halaman, dalam urutan halaman.

Aturan untuk setiap skrip:
- Narasi mengalir untuk dibacakan oleh pembicara (bukan poin-poin).
- Slide pembuka / pemisah bab: 1-2 kalimat singkat untuk transisi halus.
- Slide isi: bahas materi secara MENDALAM, bukan sekadar mengulang poin di
  slide. Targetkan sekitar 6-12 kalimat untuk slide isi yang padat.
- Untuk memperdalam pembahasan, gunakan cara yang paling sesuai dengan isi
  slide (pilih yang relevan, jangan paksakan semuanya):
  * Beri CONTOH atau ilustrasi konkret / studi kasus kecil untuk menjelaskan
    konsep abstrak (mis. nilai contoh, skenario penggunaan nyata, instance
    dari sebuah kelas atau pola).
  * Jelaskan ALASAN / motivasi: mengapa konsep ini penting, masalah apa yang
    diselesaikan, dan apa akibatnya jika diabaikan.
  * Gunakan ANALOGI sederhana dari kehidupan sehari-hari bila membantu
    pemahaman.
  * Uraikan IMPLIKASI, kelebihan/kekurangan, atau trade-off yang relevan.
  * Hubungkan dengan konsep di slide sebelumnya sehingga terbentuk alur
    pemahaman yang utuh.
- Slide kode: jelaskan tujuan kelas / method dan logika kuncinya tanpa
  membaca seluruh kode. Perdalam dengan menelusuri satu contoh eksekusi
  konkret: misalkan input atau objek tertentu, lalu jelaskan apa yang terjadi
  langkah demi langkah dan hasil akhirnya.
- Transisi antar slide harus terasa halus. JANGAN sebut nomor slide
  ("slide 1", "halaman 5", dsb).
- Tetap setia pada isi slide; contoh tambahan boleh untuk memperjelas, tetapi
  jangan mengarang materi yang bertentangan dengan slide.
- Eja angka dan simbol dalam Bahasa Indonesia jika muncul dalam kalimat
  (mis. "lima tambah tiga", bukan "5+3").
- Tanpa markup, judul, atau heading - hanya teks narasi murni.

Output: HANYA satu objek JSON valid, tanpa teks pembuka atau penutup,
tanpa code fence. Bentuk:

{{
  "scripts": [
    "narasi untuk halaman 1 ...",
    "narasi untuk halaman 2 ...",
    ...
  ]
}}

Panjang array "scripts" harus tepat {n}.
"""


def stage_generate_scripts(
    pdf: Path,
    scripts_dir: Path,
    page_count: int,
    claude_cmd: str,
    claude_model: str,
    claude_effort: str,
    force: bool,
) -> None:
    scripts_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(scripts_dir.glob("slide_*.txt"))
    if not force and len(existing) >= page_count:
        log(f"[2/6] scripts/ already has {len(existing)} files — skipping generation")
        return

    if shutil.which(claude_cmd) is None:
        sys.exit(
            f"[fatal] `{claude_cmd}` CLI not found in PATH.\n"
            "        Install Claude Code (https://claude.com/claude-code) "
            "or pre-populate the scripts directory manually."
        )

    prompt = SCRIPT_PROMPT_TEMPLATE.format(pdf=pdf, n=page_count)

    cmd = [
        claude_cmd, "-p",
        "--output-format", "stream-json",
        "--verbose",  # required by --output-format=stream-json
        "--allowedTools", "Read",
        "--add-dir", str(pdf.parent),
        "--permission-mode", "bypassPermissions",
    ]
    if claude_model:
        cmd += ["--model", claude_model]
    if claude_effort:
        cmd += ["--effort", claude_effort]
    log(
        f"[2/6] Generating {page_count} narration scripts via `{claude_cmd}` "
        f"(model={claude_model or 'default'}, effort={claude_effort or 'default'})..."
    )
    raw_path = scripts_dir / "_raw_response.txt"
    raw_lines: list[str] = []

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert proc.stdin and proc.stdout and proc.stderr

    # Heartbeat: print a "...still working" line periodically so the user
    # knows the process is alive between meaningful events.
    stop_heartbeat = threading.Event()

    def _heartbeat() -> None:
        last = time.monotonic()
        while not stop_heartbeat.wait(15):
            log(f"[2/6] ...still generating ({int(time.monotonic() - last)}s since last event)")

    hb = threading.Thread(target=_heartbeat, daemon=True)
    hb.start()

    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except BrokenPipeError:
        pass

    inner_text = ""
    deadline = time.monotonic() + 1800  # 30 min hard cap

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            raw_lines.append(line)
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = ev.get("type")
            subtype = ev.get("subtype")
            if etype == "system":
                model = ev.get("model") or ev.get("session_id", "")
                log(f"[2/6] claude session started ({model})")
            elif etype == "assistant":
                msg = ev.get("message", {})
                for block in msg.get("content", []) or []:
                    btype = block.get("type")
                    if btype == "text":
                        snippet = (block.get("text") or "").strip().replace("\n", " ")
                        if snippet:
                            log(f"[2/6] assistant text: {snippet[:80]}"
                                + ("..." if len(snippet) > 80 else ""))
                    elif btype == "tool_use":
                        name = block.get("name", "?")
                        inp = block.get("input", {}) or {}
                        target = inp.get("file_path") or inp.get("path") or ""
                        log(f"[2/6] tool: {name} {target}")
            elif etype == "user":
                # Tool results echoed back; surface concise summary.
                msg = ev.get("message", {})
                for block in msg.get("content", []) or []:
                    if block.get("type") == "tool_result":
                        is_err = block.get("is_error")
                        log(f"[2/6] tool result {'error' if is_err else 'ok'}")
            elif etype == "result":
                if subtype and subtype != "success":
                    log(f"[2/6] result subtype={subtype}")
                inner_text = ev.get("result", "") or inner_text
            if time.monotonic() > deadline:
                proc.kill()
                sys.exit("[fatal] claude CLI exceeded 30-minute deadline")
    finally:
        stop_heartbeat.set()

    stderr_text = proc.stderr.read() if proc.stderr else ""
    rc = proc.wait()

    if rc != 0:
        raw_path.write_text("\n".join(raw_lines) + "\n---STDERR---\n" + stderr_text,
                            encoding="utf-8")
        sys.exit(
            f"[fatal] claude CLI failed (exit {rc}); raw saved to {raw_path}\n"
            f"stderr: {stderr_text.strip()[:500]}"
        )

    if not inner_text:
        raw_path.write_text("\n".join(raw_lines), encoding="utf-8")
        sys.exit(f"[fatal] No result emitted by claude; raw saved to {raw_path}")

    # Extract JSON object from inner_text (model may wrap in code fences).
    m = re.search(r"\{.*\}", inner_text, re.DOTALL)
    if not m:
        raw_path.write_text("\n".join(raw_lines), encoding="utf-8")
        sys.exit(f"[fatal] No JSON object in claude output; raw saved to {raw_path}")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raw_path.write_text("\n".join(raw_lines), encoding="utf-8")
        sys.exit(f"[fatal] JSON parse failed: {e}; raw saved to {raw_path}")

    scripts = data.get("scripts", [])
    if not isinstance(scripts, list) or len(scripts) != page_count:
        raw_path.write_text("\n".join(raw_lines), encoding="utf-8")
        got = len(scripts) if isinstance(scripts, list) else type(scripts).__name__
        sys.exit(f"[fatal] Expected {page_count} scripts, got {got}. Raw saved to {raw_path}.")

    width = max(2, len(str(page_count)))
    for i, text in enumerate(scripts, 1):
        path = scripts_dir / f"slide_{i:0{width}d}.txt"
        path.write_text((str(text) or "").strip() + "\n", encoding="utf-8")
    log(f"[2/6] Wrote {page_count} scripts to {scripts_dir}")


# ---------------------------------------------------------------------------
# Stage 2 (alt narrator): Generate narration scripts via the `codex` CLI
#
# Unlike the claude narrator (which reads the PDF binary directly), codex works
# from pdftotext-extracted slide text fed through a JSON output schema.
# ---------------------------------------------------------------------------


CODEX_PROMPT_HEADER = textwrap.dedent(
    """
    You are generating narration scripts for an Indonesian lecture video from extracted PDF slide text.

    Requirements:
    - Write in Indonesian.
    - Return one narration script for every slide, preserving the slide number.
    - Do not say "slide 1", "slide 2", or similar slide-number narration.
    - Make transitions smooth between consecutive slides.
    - Discuss the slide content in DEPTH; do not merely read bullet points.
      For each content slide, deepen the explanation using whichever of
      these techniques best fit the material (pick the relevant ones, do
      not force all of them):
      * Give a concrete EXAMPLE or instance to illustrate an abstract
        concept (e.g. sample values, a real usage scenario, an instance of
        a class or design pattern).
      * Explain the REASONING: why the concept matters, what problem it
        solves, and the consequence of ignoring it.
      * Use a simple everyday ANALOGY when it aids understanding.
      * Spell out IMPLICATIONS, advantages/disadvantages, or trade-offs.
      * Connect the idea to the previous slide so the lecture builds a
        coherent line of thought.
    - For code slides, explain the purpose and flow of the code clearly,
      then deepen it by walking through one concrete execution: assume a
      specific input or object and describe step by step what happens and
      the final result.
    - Aim for richer spoken narration, roughly 70-150 words for content
      slides; keep title or section-divider slides short (1-2 sentences).
    - Spell out numbers and symbols in Indonesian words when they appear in a
      sentence (e.g. "lima tambah tiga", not "5+3").
    - Use plain text only in each script. No Markdown, no code fences, no bullet lists.
    - You may add clarifying examples, but do not invent material that
      contradicts the extracted slide text.
    - Output must match the JSON schema exactly.

    Extracted slide text:
    """
).strip()


def _extract_page_text(pdf: Path, page: int, total_pages: int, pdftotext: str) -> list[str]:
    out = subprocess.check_output(
        [pdftotext, "-layout", "-f", str(page), "-l", str(page), str(pdf), "-"],
        text=True,
    )
    cleaned: list[str] = []
    for line in out.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        if re.fullmatch(rf"{page}\s*/\s*{total_pages}", line):
            continue
        if re.fullmatch(r"\d+\s*/\s*\d+", line):
            continue
        cleaned.append(line)
    return cleaned


def _codex_schema(total_pages: int) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "scripts": {
                "type": "array",
                "minItems": total_pages,
                "maxItems": total_pages,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "slide": {"type": "integer", "minimum": 1, "maximum": total_pages},
                        "text": {"type": "string", "minLength": 40},
                    },
                    "required": ["slide", "text"],
                },
            }
        },
        "required": ["scripts"],
    }


def _codex_prompt(slides: list[dict]) -> str:
    blocks = []
    for s in slides:
        n = int(s["slide"])
        text = str(s["text"]).strip() or "(Tidak ada teks yang berhasil diekstrak.)"
        blocks.append(f'<slide number="{n:02d}">\n{text}\n</slide>')
    return CODEX_PROMPT_HEADER + "\n\n" + "\n\n".join(blocks)


def _load_codex_response(path: Path) -> dict:
    text = path.read_text(encoding="utf-8").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise
        return json.loads(m.group(0))


def _codex_error_details(exc: BaseException) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        output = "\n".join(p.strip() for p in [exc.stdout or "", exc.stderr or ""] if p and p.strip())
        m = re.search(r'"message"\s*:\s*"([^"]+)"', output)
        if m:
            return f"{exc}\n{m.group(1)}"
        if output:
            return f"{exc}\nCommand output tail:\n{output[-2000:]}"
    return str(exc)


def stage_generate_scripts_codex(
    pdf: Path,
    scripts_dir: Path,
    work_dir: Path,
    page_count: int,
    codex_cmd: str,
    codex_model: str,
    reasoning_effort: str,
    retries: int,
    retry_wait: float,
    timeout: float,
    force: bool,
) -> None:
    scripts_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(scripts_dir.glob("slide_*.txt"))
    if not force and len(existing) >= page_count:
        log(f"[2/6] scripts/ already has {len(existing)} files — skipping generation")
        return

    need_bin("pdftotext")
    pdftotext = shutil.which("pdftotext")
    codex_bin = shutil.which(codex_cmd)
    if codex_bin is None:
        sys.exit(
            f"[fatal] `{codex_cmd}` CLI not found in PATH.\n"
            "        Install/login to Codex CLI (npm install -g @openai/codex), "
            "or pre-populate the scripts directory and use --skip-scripts."
        )

    log(f"[2/6] Extracting slide text for {page_count} pages via pdftotext...")
    slides = []
    for page in range(1, page_count + 1):
        lines = _extract_page_text(pdf, page, page_count, pdftotext)
        slides.append({"slide": page, "text": "\n".join(lines).strip()})

    work_dir.mkdir(parents=True, exist_ok=True)
    schema_path = work_dir / "codex_scripts_schema.json"
    output_path = work_dir / "codex_scripts_response.json"
    schema_path.write_text(json.dumps(_codex_schema(page_count), indent=2), encoding="utf-8")
    prompt = _codex_prompt(slides)

    width = max(2, len(str(page_count)))
    log(
        f"[2/6] Generating {page_count} narration scripts via `{codex_cmd}` "
        f"(model={codex_model}, effort={reasoning_effort})..."
    )

    for attempt in range(1, retries + 2):
        if output_path.exists():
            output_path.unlink()
        log(f"[2/6] codex attempt {attempt}/{retries + 1}...")
        try:
            subprocess.run(
                [
                    codex_bin, "exec",
                    "--model", codex_model,
                    "-c", f'model_reasoning_effort="{reasoning_effort}"',
                    "--sandbox", "read-only",
                    "--skip-git-repo-check",
                    "--ephemeral",
                    "--output-schema", str(schema_path),
                    "--output-last-message", str(output_path),
                    "-",
                ],
                input=prompt,
                text=True,
                capture_output=True,
                check=True,
                timeout=timeout,
            )
            data = _load_codex_response(output_path)
            scripts = data.get("scripts")
            if not isinstance(scripts, list) or len(scripts) != page_count:
                got = len(scripts) if isinstance(scripts, list) else "no"
                raise RuntimeError(f"codex returned {got} scripts; expected {page_count}.")

            by_slide: dict[int, str] = {}
            for item in scripts:
                if not isinstance(item, dict):
                    raise RuntimeError("codex returned a non-object script item.")
                num = int(item["slide"])
                text = str(item["text"]).strip()
                if not text:
                    raise RuntimeError(f"codex returned an empty script for slide {num}.")
                by_slide[num] = text

            for page in range(1, page_count + 1):
                if page not in by_slide:
                    raise RuntimeError(f"codex response is missing slide {page}.")
                path = scripts_dir / f"slide_{page:0{width}d}.txt"
                path.write_text(by_slide[page] + "\n", encoding="utf-8")
            log(f"[2/6] Wrote {page_count} scripts to {scripts_dir}")
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                RuntimeError, json.JSONDecodeError) as exc:
            details = _codex_error_details(exc)
            if attempt > retries:
                hint = ""
                if "requires a newer version of Codex" in details:
                    hint = ("\n\nThe installed Codex CLI is too old for the requested model. "
                            "Upgrade with:\n  npm install -g @openai/codex@latest")
                sys.exit(
                    f"[fatal] codex script generation failed after {retries + 1} attempt(s).\n"
                    f"Last error: {details}{hint}"
                )
            log(f"[2/6] codex failed ({details[:200]}); retrying in {retry_wait:.0f}s...")
            time.sleep(retry_wait)


# ---------------------------------------------------------------------------
# Stage 3 + 5: TTS (audio + SRT in one call) via edge-tts with retries
# ---------------------------------------------------------------------------


async def _edge_tts_one(text: str, voice: str, rate: str, mp3: Path, srt: Path) -> None:
    """Single edge-tts call; raises on any error so the caller can retry.

    Edge TTS emits the MP3 and real word/sentence-boundary timing together, so
    its SRT cues are precisely aligned to the audio.
    """
    communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate)
    submaker = edge_tts.SubMaker()
    tmp_mp3 = mp3.with_suffix(mp3.suffix + ".part")
    try:
        with open(tmp_mp3, "wb") as f:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
                elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                    submaker.feed(chunk)
        if tmp_mp3.stat().st_size == 0:
            raise RuntimeError("edge-tts produced 0-byte audio")
        tmp_mp3.replace(mp3)
        srt.write_text(submaker.get_srt(), encoding="utf-8")
    except BaseException:
        # Clean partial file so a retry/resume starts fresh.
        try:
            tmp_mp3.unlink()
        except FileNotFoundError:
            pass
        raise


async def _edge_tts_one_with_retry(
    label: str, text: str, voice: str, rate: str, mp3: Path, srt: Path,
    retries: int, wait: float,
) -> None:
    attempts = retries + 1  # initial try + N retries
    last_err: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            await _edge_tts_one(text, voice, rate, mp3, srt)
            return
        except BaseException as e:  # noqa: BLE001
            last_err = e
            if attempt >= attempts:
                break
            log(
                f"   [tts retry] {label}: attempt {attempt}/{attempts} failed "
                f"({type(e).__name__}: {e}); waiting {wait}s..."
            )
            await asyncio.sleep(wait)
    raise RuntimeError(f"TTS failed for {label} after {attempts} attempts: {last_err}") from last_err


async def _edge_tts_many(jobs, voice: str, rate: str, concurrency: int,
                         retries: int, wait: float) -> None:
    sem = asyncio.Semaphore(concurrency)
    total = len(jobs)
    done = {"n": 0}

    async def worker(label, text, mp3, srt):
        async with sem:
            await _edge_tts_one_with_retry(label, text, voice, rate, mp3, srt, retries, wait)
        done["n"] += 1
        log(f"   [tts] {done['n']}/{total} {label}")

    await asyncio.gather(*(worker(*j) for j in jobs))


# --- Gemini TTS ------------------------------------------------------------
#
# The Gemini API returns raw PCM audio only (no timing events), so we pipe the
# PCM through ffmpeg into MP3 and estimate the SRT by splitting the script into
# sentences and allocating time proportionally to sentence length.

GEMINI_TTS_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _write_estimated_srt(text: str, duration: float, srt: Path) -> None:
    sentences = _split_sentences(text)
    if not sentences:
        srt.write_text("", encoding="utf-8")
        return
    weights = [max(1, len(s)) for s in sentences]
    total = sum(weights) or 1
    elapsed = 0
    pieces: list[str] = []
    for idx, (sentence, weight) in enumerate(zip(sentences, weights), start=1):
        start = duration * elapsed / total
        elapsed += weight
        end = duration * elapsed / total
        if idx == len(sentences):
            end = duration
        pieces.append(f"{idx}\n{_fmt_ts(start)} --> {_fmt_ts(end)}\n{sentence}\n")
    srt.write_text("\n".join(pieces), encoding="utf-8")


def _gemini_synth(text: str, api_key: str, model: str, voice: str,
                  timeout: float) -> tuple[bytes, int]:
    """Return (pcm_s16le_bytes, sample_rate). Raises on any failure."""
    body = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}
            },
        },
    }
    req = urllib.request.Request(
        GEMINI_TTS_ENDPOINT.format(model=model),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"gemini HTTP {e.code}: {detail}") from e
    try:
        inline = data["candidates"][0]["content"]["parts"][0]["inlineData"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"gemini: no audio in response: {str(data)[:400]}") from e
    pcm = base64.b64decode(inline["data"])
    rate = 24000
    for tok in inline.get("mimeType", "").split(";"):
        if "rate=" in tok:
            try:
                rate = int(tok.split("rate=")[1])
            except ValueError:
                pass
    return pcm, rate


def _gemini_tts_one(text: str, mp3: Path, srt: Path, api_key: str, model: str,
                    voice: str, timeout: float) -> None:
    pcm, rate = _gemini_synth(text, api_key, model, voice, timeout)
    if not pcm:
        raise RuntimeError("gemini produced 0-byte audio")
    tmp_mp3 = mp3.with_suffix(mp3.suffix + ".part")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
             "-c:a", "libmp3lame", "-q:a", "2", "-f", "mp3", str(tmp_mp3)],
            input=pcm, check=True,
        )
        if tmp_mp3.stat().st_size == 0:
            raise RuntimeError("ffmpeg produced 0-byte mp3")
        tmp_mp3.replace(mp3)
    except BaseException:
        try:
            tmp_mp3.unlink()
        except FileNotFoundError:
            pass
        raise
    _write_estimated_srt(text, _ffprobe_duration(mp3), srt)


def _gemini_tts_many(jobs, api_key: str, model: str, voice: str, concurrency: int,
                     retries: int, wait: float, timeout: float) -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    total = len(jobs)
    done = {"n": 0}
    lock = threading.Lock()

    def worker(label, text, mp3, srt):
        attempts = retries + 1
        last_err: BaseException | None = None
        for attempt in range(1, attempts + 1):
            try:
                _gemini_tts_one(text, mp3, srt, api_key, model, voice, timeout)
                with lock:
                    done["n"] += 1
                    n = done["n"]
                log(f"   [tts] {n}/{total} {label}")
                return
            except BaseException as e:  # noqa: BLE001
                last_err = e
                if attempt >= attempts:
                    break
                log(
                    f"   [tts retry] {label}: attempt {attempt}/{attempts} failed "
                    f"({type(e).__name__}: {e}); waiting {wait}s..."
                )
                time.sleep(wait)
        raise RuntimeError(f"TTS failed for {label} after {attempts} attempts: {last_err}")

    errors: list[BaseException] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futs = {pool.submit(worker, *j): j for j in jobs}
        for fut in as_completed(futs):
            try:
                fut.result()
            except BaseException as e:  # noqa: BLE001
                errors.append(e)
    if errors:
        sys.exit(f"[fatal] gemini TTS failed: {errors[0]}")


def stage_tts(
    scripts_dir: Path,
    audio_dir: Path,
    subs_dir: Path,
    page_count: int,
    provider: str,
    voice: str,
    rate: str,
    gemini_model: str,
    gemini_voice: str,
    gemini_api_key: str | None,
    concurrency: int,
    force: bool,
    skip_existing_audio: bool,
    retries: int,
    retry_wait: float,
    tts_timeout: float,
) -> None:
    audio_dir.mkdir(parents=True, exist_ok=True)
    subs_dir.mkdir(parents=True, exist_ok=True)
    width = max(2, len(str(page_count)))

    jobs = []
    for i in range(1, page_count + 1):
        stem = f"{i:0{width}d}"
        txt = scripts_dir / f"slide_{stem}.txt"
        mp3 = audio_dir / f"slide_{stem}.mp3"
        srt = subs_dir / f"clip_{stem}.srt"
        if not txt.exists():
            sys.exit(f"[fatal] Missing script: {txt}")

        if force:
            pass  # always regenerate
        elif skip_existing_audio and mp3.exists() and mp3.stat().st_size > 0:
            # Keep existing audio even if SRT is missing/old.
            continue
        elif mp3.exists() and mp3.stat().st_size > 0 and srt.exists():
            continue

        jobs.append((f"slide_{stem}", txt.read_text(encoding="utf-8").strip(), mp3, srt))

    if not jobs:
        log(f"[3/6+5/6] All {page_count} audio+srt files present — skipping TTS")
        return

    if provider == "gemini":
        need_bin("ffmpeg")
        need_bin("ffprobe")
        if not gemini_api_key:
            sys.exit(
                "[fatal] gemini TTS requires an API key. Set GEMINI_API_KEY in the "
                "environment or in a .env at the repo root, or pass --gemini-api-key."
            )
        log(
            f"[3/6+5/6] Generating {len(jobs)} audio+srt via Gemini TTS "
            f"(model={gemini_model}, voice={gemini_voice}, retries={retries}, "
            f"wait={retry_wait}s, concurrency={concurrency}). SRT timings are estimated."
        )
        _gemini_tts_many(jobs, gemini_api_key, gemini_model, gemini_voice,
                         concurrency, retries, retry_wait, tts_timeout)
    else:
        log(
            f"[3/6+5/6] Generating {len(jobs)} audio+srt via edge-tts "
            f"(voice={voice}, rate={rate}, retries={retries}, wait={retry_wait}s, "
            f"concurrency={concurrency})"
        )
        asyncio.run(_edge_tts_many(jobs, voice, rate, concurrency, retries, retry_wait))
    log(f"[3/6+5/6] TTS complete: {audio_dir}, {subs_dir}")


# ---------------------------------------------------------------------------
# Stage 4: Per-slide clips
# ---------------------------------------------------------------------------


def _ffprobe_duration(path: Path) -> float:
    out = subprocess.check_output([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        str(path),
    ]).decode().strip()
    return float(out)


def stage_clips(
    slides_dir: Path,
    audio_dir: Path,
    clips_dir: Path,
    page_count: int,
    width_px: int,
    height_px: int,
    concurrency: int,
    force: bool,
    crf: int,
    preset: str,
    audio_bitrate: str,
) -> None:
    need_bin("ffmpeg")
    need_bin("ffprobe")
    clips_dir.mkdir(parents=True, exist_ok=True)
    width = max(2, len(str(page_count)))

    vf = (
        f"scale={width_px}:{height_px}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={width_px}:{height_px}:(ow-iw)/2:(oh-ih)/2:white,"
        "setsar=1,format=yuv420p"
    )

    def make_one(i: int):
        stem = f"{i:0{width}d}"
        png = slides_dir / f"slide_{stem}.png"
        mp3 = audio_dir / f"slide_{stem}.mp3"
        out = clips_dir / f"clip_{stem}.mp4"
        if not png.exists():
            return i, False, f"missing {png}"
        if not mp3.exists():
            return i, False, f"missing {mp3}"
        if not force and out.exists():
            try:
                _ffprobe_duration(out)
                return i, True, "skip"
            except Exception:
                pass  # corrupt -> regenerate
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", str(png),
            "-i", str(mp3),
            "-vf", vf,
            "-c:v", "libx264", "-tune", "stillimage",
            "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", audio_bitrate, "-ac", "2", "-ar", "48000",
            "-shortest", "-movflags", "+faststart",
            str(out),
        ]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return i, True, "ok"
        except subprocess.CalledProcessError as e:
            return i, False, str(e)

    from concurrent.futures import ThreadPoolExecutor, as_completed

    log(
        f"[4/6] Encoding {page_count} clips at {width_px}x{height_px} "
        f"(crf={crf}, preset={preset}, audio={audio_bitrate}, concurrency={concurrency})"
    )
    failures = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futs = {pool.submit(make_one, i): i for i in range(1, page_count + 1)}
        done = 0
        for fut in as_completed(futs):
            i, ok, info = fut.result()
            done += 1
            if not ok:
                failures.append((i, info))
            log(f"   [clip] {done}/{page_count} clip_{i:0{max(2, len(str(page_count)))}d} ({info})")
    if failures:
        for i, info in failures:
            log(f"   [fail] clip {i}: {info}")
        sys.exit("[fatal] one or more clips failed to encode")
    log(f"[4/6] All {page_count} clips encoded")


# ---------------------------------------------------------------------------
# Stage 6: Merge clips + SRTs
# ---------------------------------------------------------------------------

_TS = re.compile(r"(\d+):(\d+):(\d+),(\d+)")


def _parse_ts(s: str) -> float:
    m = _TS.match(s.strip())
    if not m:
        raise ValueError(f"bad timestamp: {s}")
    h, mi, se, ms = map(int, m.groups())
    return h * 3600 + mi * 60 + se + ms / 1000.0


def _fmt_ts(t: float) -> str:
    if t < 0:
        t = 0.0
    h = int(t // 3600); t -= h * 3600
    m = int(t // 60); t -= m * 60
    s = int(t); ms = int(round((t - s) * 1000))
    if ms >= 1000:
        s += 1; ms -= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _parse_srt(path: Path):
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    out = []
    for blk in re.split(r"\n\s*\n", raw):
        lines = blk.strip().splitlines()
        ts_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if ts_idx is None:
            continue
        m = re.match(r"\s*(\d+:\d+:\d+,\d+)\s*-->\s*(\d+:\d+:\d+,\d+)", lines[ts_idx])
        if not m:
            continue
        s = _parse_ts(m.group(1))
        e = _parse_ts(m.group(2))
        text = "\n".join(lines[ts_idx + 1:]).strip()
        out.append((s, e, text))
    return out


def stage_merge(
    clips_dir: Path,
    subs_dir: Path,
    work_dir: Path,
    out_mp4: Path,
    out_srt: Path,
    page_count: int,
) -> None:
    need_bin("ffmpeg")
    work_dir.mkdir(parents=True, exist_ok=True)
    width = max(2, len(str(page_count)))

    concat_list = work_dir / "concat.txt"
    rels = []
    for i in range(1, page_count + 1):
        stem = f"{i:0{width}d}"
        clip = clips_dir / f"clip_{stem}.mp4"
        if not clip.exists():
            sys.exit(f"[fatal] missing clip {clip}")
        rels.append(os.path.relpath(clip, work_dir))
    concat_list.write_text("\n".join(f"file '{r}'" for r in rels) + "\n", encoding="utf-8")

    log(f"[6/6] Concatenating {page_count} clips -> {out_mp4}")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
         "-c", "copy", "-movflags", "+faststart", str(out_mp4)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    log(f"[6/6] Merging subtitles -> {out_srt}")
    idx = 1
    offset = 0.0
    pieces: list[str] = []
    for i in range(1, page_count + 1):
        stem = f"{i:0{width}d}"
        clip = clips_dir / f"clip_{stem}.mp4"
        srt = subs_dir / f"clip_{stem}.srt"
        dur = _ffprobe_duration(clip)
        last_end = 0.0
        for s, e, text in _parse_srt(srt):
            s = max(s, last_end)
            e = max(e, s + 0.1)
            if e > dur:
                e = dur
            if s >= dur:
                continue
            pieces.append(
                f"{idx}\n{_fmt_ts(offset + s)} --> {_fmt_ts(offset + e)}\n{text}\n"
            )
            idx += 1
            last_end = e
        offset += dur
    out_srt.write_text("\n".join(pieces), encoding="utf-8")
    log(f"[done] {out_mp4} ({offset:.1f}s) and {out_srt} ({idx-1} cues)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

STAGES = ["pdf", "scripts", "audio", "clips", "merge"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate a narrated lecture video from a PDF slide deck.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--pdf", required=True, type=Path, help="Input PDF slide deck.")
    p.add_argument("--target", required=True, type=Path,
                   help="Target output directory (will be created).")
    p.add_argument("--final-name", default=None,
                   help="Stem for the final mp4/srt (defaults to PDF stem).")

    p.add_argument("--tts-provider", default="edge", choices=["edge", "gemini"],
                   help="Text-to-speech engine (default: edge). "
                        "edge is free/no-key; gemini needs an API key and gives "
                        "estimated (not exact) subtitle timing.")
    p.add_argument("--voice", default="id-ID-ArdiNeural",
                   help="edge-tts voice (default: id-ID-ArdiNeural; also try id-ID-GadisNeural).")
    p.add_argument("--rate", default="-5%",
                   help="edge-tts rate adjust (e.g. -5%%, +0%%, +10%%).")
    p.add_argument("--gemini-voice", default="Iapetus",
                   help="Gemini TTS prebuilt voice (default: Iapetus; also Charon, Orus, ...).")
    p.add_argument("--gemini-tts-model", default="gemini-2.5-flash-preview-tts",
                   help="Gemini TTS model (default: gemini-2.5-flash-preview-tts).")
    p.add_argument("--gemini-api-key", default=None,
                   help="Gemini API key. Defaults to $GEMINI_API_KEY or a .env at the repo root.")
    p.add_argument("--skip-existing-audio", action="store_true",
                   help="Skip TTS for any slide whose MP3 already exists, "
                        "even if its SRT is missing.")
    p.add_argument("--tts-retries", type=int, default=3,
                   help="Max TTS retries per slide on failure (default 3).")
    p.add_argument("--tts-retry-wait", type=float, default=10.0,
                   help="Seconds to wait between TTS retries (default 10).")
    p.add_argument("--tts-timeout", type=float, default=180.0,
                   help="Seconds before one Gemini TTS request is considered stuck (default 180).")

    p.add_argument("--dpi", type=int, default=300, help="PDF render DPI (default 300).")
    p.add_argument("--width", type=int, default=1920, help="Output video width (default 1920 / 1080p).")
    p.add_argument("--height", type=int, default=1080, help="Output video height (default 1080 / 1080p).")
    p.add_argument("--crf", type=int, default=14,
                   help="libx264 CRF; lower = higher quality (default 14 ≈ visually lossless). "
                        "Set 0 for mathematically lossless.")
    p.add_argument("--preset", default="slow",
                   choices=["ultrafast","superfast","veryfast","faster","fast",
                            "medium","slow","slower","veryslow"],
                   help="libx264 preset; slower = better compression at same CRF (default slow).")
    p.add_argument("--audio-bitrate", default="256k",
                   help="AAC audio bitrate (default 256k).")
    p.add_argument("--concurrency", type=int, default=6,
                   help="Parallel TTS / ffmpeg workers (default 6).")
    p.add_argument("--narrator", default="claude", choices=["claude", "codex"],
                   help="Narration script generator (default: claude). "
                        "codex feeds pdftotext-extracted slide text to `codex exec`.")
    p.add_argument("--claude-cmd", default="claude",
                   help="Claude CLI executable for script generation (default: claude).")
    p.add_argument("--claude-model", default="opus",
                   help="Model alias or ID passed to `claude --model` (default: opus).")
    p.add_argument("--claude-effort", default="high",
                   choices=["low", "medium", "high", "xhigh", "max", ""],
                   help="Effort level passed to `claude --effort` (default: high). Pass '' to omit.")
    p.add_argument("--codex-cmd", default="codex",
                   help="Codex CLI executable for script generation (default: codex).")
    p.add_argument("--codex-model", default="gpt-5.5",
                   help="Model passed to `codex exec --model` (default: gpt-5.5).")
    p.add_argument("--codex-reasoning-effort", default="xhigh",
                   help="Reasoning effort for codex (model_reasoning_effort; default: xhigh).")
    p.add_argument("--codex-retries", type=int, default=2,
                   help="Max codex retries on transient failure (default 2).")
    p.add_argument("--codex-retry-wait", type=float, default=30.0,
                   help="Seconds to wait between codex retries (default 30).")
    p.add_argument("--codex-timeout", type=float, default=1800.0,
                   help="Seconds before one codex attempt is considered stuck (default 1800).")
    p.add_argument("--only", default=None,
                   help=f"Comma-separated stages to run: {','.join(STAGES)}.")
    p.add_argument("--skip-scripts", action="store_true",
                   help="Use existing scripts/*.txt (no API call).")
    p.add_argument("--force", action="store_true",
                   help="Regenerate outputs even if they already exist.")
    return p.parse_args()


def _resolve_gemini_key(cli_value: str | None) -> str | None:
    """Resolve the Gemini API key from --gemini-api-key, $GEMINI_API_KEY, then a
    .env at the repo root (APP_DIR/.. , i.e. the slides_narrator project root)."""
    if cli_value:
        return cli_value
    env_value = os.environ.get("GEMINI_API_KEY")
    if env_value:
        return env_value
    env_file = APP_DIR.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key.strip() == "GEMINI_API_KEY":
                return val.strip().strip('"').strip("'")
    return None


def main() -> None:
    args = parse_args()
    pdf = args.pdf.resolve()
    target = args.target.resolve()
    if not pdf.exists():
        sys.exit(f"[fatal] PDF not found: {pdf}")
    target.mkdir(parents=True, exist_ok=True)

    slides_dir = target / "slides"
    scripts_dir = target / "scripts"
    audio_dir = target / "audio"
    clips_dir = target / "clips"
    subs_dir = target / "subtitles"
    work_dir = target / "work"

    final_stem = args.final_name or pdf.stem
    out_mp4 = target / f"{final_stem}.mp4"
    out_srt = target / f"{final_stem}.srt"

    stages = set(STAGES if args.only is None else
                 [s.strip() for s in args.only.split(",") if s.strip()])
    bad = stages - set(STAGES)
    if bad:
        sys.exit(f"[fatal] unknown stage(s) in --only: {bad}")

    if "pdf" in stages:
        page_count = stage_pdf_to_pngs(pdf, slides_dir, dpi=args.dpi)
    else:
        page_count = get_page_count(pdf)

    if "scripts" in stages and not args.skip_scripts:
        if args.narrator == "codex":
            stage_generate_scripts_codex(
                pdf, scripts_dir, work_dir, page_count,
                args.codex_cmd, args.codex_model, args.codex_reasoning_effort,
                args.codex_retries, args.codex_retry_wait, args.codex_timeout,
                args.force,
            )
        else:
            stage_generate_scripts(
                pdf, scripts_dir, page_count,
                args.claude_cmd, args.claude_model, args.claude_effort, args.force,
            )
    elif args.skip_scripts:
        existing = sorted(scripts_dir.glob("slide_*.txt"))
        if len(existing) < page_count:
            sys.exit(
                f"[fatal] --skip-scripts set but only found {len(existing)} of "
                f"{page_count} scripts in {scripts_dir}"
            )

    if "audio" in stages:
        gemini_key = _resolve_gemini_key(args.gemini_api_key) if args.tts_provider == "gemini" else None
        stage_tts(
            scripts_dir, audio_dir, subs_dir, page_count,
            args.tts_provider, args.voice, args.rate,
            args.gemini_tts_model, args.gemini_voice, gemini_key,
            args.concurrency, args.force,
            args.skip_existing_audio, args.tts_retries, args.tts_retry_wait,
            args.tts_timeout,
        )

    if "clips" in stages:
        stage_clips(slides_dir, audio_dir, clips_dir, page_count,
                    args.width, args.height, args.concurrency, args.force,
                    args.crf, args.preset, args.audio_bitrate)

    if "merge" in stages:
        stage_merge(clips_dir, subs_dir, work_dir, out_mp4, out_srt, page_count)


if __name__ == "__main__":
    main()
