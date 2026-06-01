#!/usr/bin/env python3
"""Build a narrated lecture video from a PDF slide deck.

Pipeline:
PDF pages -> slide images -> narration scripts -> MP3 narration -> per-slide
clips and subtitles -> final MP4 and SRT.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
VENV_DIR = APP_DIR / ".venv"
REQ_FILE = APP_DIR / "requirements.txt"
STARTED_AT = time.monotonic()
COMMAND_VERBOSE = False


@dataclass(frozen=True)
class VideoSettings:
    quality: str
    codec: str
    crf: int
    preset: str
    pixel_format: str
    scale_flags: str = "lanczos+accurate_rnd+full_chroma_int"


def elapsed() -> str:
    seconds = int(time.monotonic() - STARTED_AT)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def log(message: str = "") -> None:
    if message:
        print(f"[{elapsed()}] {message}", flush=True)
    else:
        print(flush=True)


def phase(message: str) -> None:
    log()
    log(f"=== {message} ===")


def progress(current: int, total: int, message: str) -> None:
    width = max(2, len(str(total)))
    log(f"[{current:0{width}d}/{total:0{width}d}] {message}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a narrated lecture video from PDF slides.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--pdf", help="Path to the PDF slide deck.")
    parser.add_argument("--target", help="Directory for all generated files.")
    parser.add_argument("--voice", default="id-ID-ArdiNeural", help="edge-tts voice name.")
    parser.add_argument("--rate", default="-5%", help="edge-tts speaking rate, for example -5%% or +0%%.")
    parser.add_argument("--dpi", type=int, default=300, help="DPI used when rendering slide images.")
    parser.add_argument("--fps", type=int, default=30, help="Frame rate for generated clips.")
    parser.add_argument("--width", type=int, default=1920, help="Output video width.")
    parser.add_argument("--height", type=int, default=1080, help="Output video height.")
    parser.add_argument(
        "--video-quality",
        choices=["lossless", "high", "standard"],
        default="lossless",
        help="Visual encoding profile for rendered slide clips.",
    )
    parser.add_argument("--video-crf", type=int, default=None, help="Override H.264 CRF. 0 is lossless; larger values are smaller/lower quality.")
    parser.add_argument("--video-preset", default=None, help="Override x264 preset, for example slow, medium, or veryfast.")
    parser.add_argument(
        "--pixel-format",
        choices=["bgr24", "rgb24", "yuv444p", "yuv420p"],
        default=None,
        help="Override output pixel format. Defaults depend on --video-quality.",
    )
    parser.add_argument("--final-name", default=None, help="Final MP4/SRT name without extension.")
    parser.add_argument("--language", default="id", choices=["id"], help="Narration script language.")
    parser.add_argument("--script-provider", choices=["template", "codex"], default="template", help="How narration scripts are generated.")
    parser.add_argument("--skip-existing-scripts", action="store_true", help="Reuse existing slide_*.txt scripts.")
    parser.add_argument("--codex-bin", default=None, help="Path to codex CLI. Defaults to the first codex on PATH.")
    parser.add_argument("--codex-model", default="gpt-5.5", help="Model for Codex script generation.")
    parser.add_argument("--codex-reasoning-effort", default="xhigh", help="Codex reasoning effort for script generation.")
    parser.add_argument("--codex-retries", type=int, default=2, help="Retry count for temporary Codex CLI/API failures.")
    parser.add_argument("--codex-retry-wait", type=float, default=30.0, help="Seconds to wait between Codex retries.")
    parser.add_argument("--codex-timeout", type=float, default=1800.0, help="Seconds before one Codex script-generation attempt is considered stuck.")
    parser.add_argument("--max-pages", type=int, default=None, help="Limit pages, useful for testing.")
    parser.add_argument("--skip-existing-audio", action="store_true", help="Reuse existing slide_*.mp3 files only when script/voice/rate metadata matches.")
    parser.add_argument("--force", action="store_true", help="Delete generated files in the target before building.")
    parser.add_argument("--tts-retries", type=int, default=5, help="Retry count for temporary edge-tts/network failures.")
    parser.add_argument("--tts-retry-wait", type=float, default=15.0, help="Seconds to wait between TTS retries.")
    parser.add_argument("--tts-timeout", type=float, default=180.0, help="Seconds before one TTS attempt is considered stuck.")
    parser.add_argument("--no-install", action="store_true", help="Use an existing edge-tts instead of installing locally.")
    parser.add_argument("--install-only", action="store_true", help="Install Python deps locally, then exit.")
    parser.add_argument("--verbose", action="store_true", help="Print every external command before running it.")
    return parser.parse_args()


def run(
    cmd: list[str | Path],
    *,
    cwd: Path | None = None,
    capture: bool = False,
    timeout: float | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    if COMMAND_VERBOSE:
        printable = " ".join(str(part) for part in cmd)
        log(f"$ {printable}")
    return subprocess.run(
        [str(part) for part in cmd],
        cwd=str(cwd) if cwd else None,
        check=True,
        text=True,
        capture_output=capture,
        timeout=timeout,
        input=input_text,
    )


def command_error_details(exc: BaseException) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        output = "\n".join(part.strip() for part in [exc.stdout or "", exc.stderr or ""] if part and part.strip())
        message_match = re.search(r'"message"\s*:\s*"([^"]+)"', output)
        if message_match:
            return f"{exc}\n{message_match.group(1)}"
        if output:
            tail = output[-4000:]
            return f"{exc}\nCommand output tail:\n{tail}"
        return str(exc)
    return str(exc)


def codex_failure_hint(details: str) -> str:
    if "requires a newer version of Codex" in details:
        return textwrap.dedent(
            """

            The installed Codex CLI is too old for the requested model.
            Upgrade it first, then rerun the same command:

              npm install -g @openai/codex@latest

            After upgrading, verify with:

              codex --version
            """
        ).rstrip()
    return ""


def local_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def local_bin(name: str) -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / f"{name}.exe"
    return VENV_DIR / "bin" / name


def install_python_deps() -> None:
    VENV_DIR.parent.mkdir(parents=True, exist_ok=True)
    if not local_python().exists():
        log(f"Creating local Python environment: {VENV_DIR}")
        run([sys.executable, "-m", "venv", VENV_DIR])
    log("Installing/updating pip in the local environment")
    run([local_python(), "-m", "pip", "install", "--upgrade", "pip"])
    log(f"Installing Python dependencies from {REQ_FILE.name}")
    run([local_python(), "-m", "pip", "install", "-r", REQ_FILE])


def edge_tts_path(no_install: bool) -> Path:
    local_edge = local_bin("edge-tts")
    if local_edge.exists():
        log(f"Using local edge-tts: {local_edge}")
        return local_edge

    if no_install:
        existing = shutil.which("edge-tts")
        if existing:
            log(f"Using system edge-tts: {existing}")
            return Path(existing)
        raise SystemExit("edge-tts was not found. Run without --no-install to install it locally.")

    log("edge-tts is not installed locally yet")
    install_python_deps()
    if not local_edge.exists():
        raise SystemExit("edge-tts installation finished, but the executable was not found.")
    return local_edge


def require_programs(names: list[str]) -> dict[str, Path]:
    log("Checking required system tools")
    found: dict[str, Path] = {}
    missing: list[str] = []
    for name in names:
        path = shutil.which(name)
        if path:
            found[name] = Path(path)
        else:
            missing.append(name)

    if missing:
        missing_text = ", ".join(missing)
        raise SystemExit(
            textwrap.dedent(
                f"""
                Missing required system tools: {missing_text}

                Install them first, then rerun this app. On Ubuntu/Debian:
                  sudo apt-get install ffmpeg poppler-utils

                Python dependencies are installed locally by this app, but ffmpeg
                and Poppler provide command-line binaries used for video rendering
                and PDF extraction.
                """
            ).strip()
        )
    return found


def codex_path(path: str | None) -> Path:
    if path:
        candidate = Path(path).expanduser()
        if candidate.exists():
            return candidate.resolve()
        found = shutil.which(path)
        if found:
            return Path(found)
        raise SystemExit(f"Codex CLI was not found: {path}")

    found = shutil.which("codex")
    if not found:
        raise SystemExit("Codex CLI was not found on PATH. Install/login to Codex CLI first, or pass --codex-bin.")
    return Path(found)


def prepare_dirs(target: Path) -> dict[str, Path]:
    dirs = {
        "root": target,
        "slides": target / "slides",
        "scripts": target / "scripts",
        "audio": target / "audio",
        "clips": target / "clips",
        "subtitles": target / "subtitles",
        "work": target / "work",
    }
    for directory in dirs.values():
        directory.mkdir(parents=True, exist_ok=True)
    log(f"Output directories are ready under {target}")
    return dirs


def clear_generated_outputs(target: Path, final_name: str | None) -> None:
    generated_dirs = ["slides", "scripts", "audio", "clips", "subtitles", "work"]
    generated_files = []
    if final_name:
        generated_files.extend([f"{final_name}.mp4", f"{final_name}.srt"])

    log(f"--force enabled; clearing generated outputs under {target}")
    target.mkdir(parents=True, exist_ok=True)
    for name in generated_dirs:
        path = target / name
        if path.exists():
            log(f"Removing {path}")
            shutil.rmtree(path)

    for name in generated_files:
        path = target / name
        if path.exists():
            log(f"Removing {path}")
            path.unlink()


def pdf_page_count(pdf: Path, pdfinfo: Path) -> int:
    output = run([pdfinfo, pdf], capture=True).stdout
    match = re.search(r"^Pages:\s+(\d+)\s*$", output, re.MULTILINE)
    if not match:
        raise SystemExit(f"Could not determine page count for {pdf}")
    return int(match.group(1))


def digits_for(total: int) -> int:
    return max(2, len(str(total)))


def render_slide_images(pdf: Path, slides_dir: Path, pdftoppm: Path, dpi: int, total_pages: int) -> None:
    prefix = slides_dir / "slide"
    log(f"Rendering {total_pages} slide image(s) at {dpi} DPI")
    run([pdftoppm, "-png", "-r", str(dpi), "-f", "1", "-l", str(total_pages), pdf, prefix])

    width = digits_for(total_pages)
    for page in range(1, total_pages + 1):
        candidates = [
            slides_dir / f"slide-{page:0{width}d}.png",
            slides_dir / f"slide-{page:02d}.png",
            slides_dir / f"slide-{page}.png",
        ]
        source = next((candidate for candidate in candidates if candidate.exists()), None)
        if source is None:
            raise SystemExit(f"Expected rendered slide image for page {page}, but none was found.")

        target = slides_dir / f"slide_{page:02d}.png"
        if source != target:
            if target.exists():
                target.unlink()
            source.rename(target)
        progress(page, total_pages, f"Slide image ready: {target.name}")


def extract_page_text(pdf: Path, page: int, total_pages: int, pdftotext: Path) -> list[str]:
    result = run([pdftotext, "-layout", "-f", str(page), "-l", str(page), pdf, "-"], capture=True)
    raw_lines = result.stdout.splitlines()
    cleaned: list[str] = []
    for line in raw_lines:
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        if re.fullmatch(rf"{page}\s*/\s*{total_pages}", line):
            continue
        if re.fullmatch(r"\d+\s*/\s*\d+", line):
            continue
        cleaned.append(line)
    return cleaned


def looks_like_section_divider(lines: list[str]) -> bool:
    meaningful = [line for line in lines if len(line) > 2]
    return len(meaningful) <= 3 and sum(len(line.split()) for line in meaningful) <= 12


def trim_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]).rstrip(".,;:") + "."


def remove_code_noise(lines: list[str]) -> list[str]:
    filtered: list[str] = []
    for line in lines:
        without_number = re.sub(r"^\d+\s+", "", line)
        if re.match(r"^(public|private|protected|import|class|interface|@Override|\}|\{)", without_number):
            continue
        filtered.append(line)
    return filtered


def script_from_lines(page: int, lines: list[str]) -> str:
    if not lines:
        return "Bagian ini menjadi transisi singkat sebelum kita melanjutkan ke materi berikutnya."

    title = lines[0]
    body_lines = lines[1:]
    body_text = " ".join(remove_code_noise(body_lines))
    body_text = re.sub(r"\s+", " ", body_text).strip()

    if page == 1:
        topic = trim_words(" ".join(lines[:4]), 24)
        return (
            f"Selamat datang pada materi {topic}. "
            "Pada sesi ini kita akan mengikuti isi slide secara bertahap, dengan fokus pada konsep utama, contoh, dan cara menerapkannya dalam pemrograman berorientasi objek."
        )

    if title.lower() in {"contents", "daftar isi"}:
        topics = trim_words(" ".join(lines[1:]), 42)
        return (
            "Alur pembahasan dimulai dari gambaran besar materi, lalu bergerak ke topik-topik utama. "
            f"Bagian yang akan dibahas meliputi {topics}. "
            "Gunakan daftar ini sebagai peta agar hubungan antar bagian lebih mudah diikuti."
        )

    if looks_like_section_divider(lines):
        topic = trim_words(" ".join(lines), 16)
        return (
            f"Kita masuk ke bagian {topic}. "
            "Pada bagian ini, perhatikan masalah desain yang ingin diselesaikan sebelum melihat struktur dan contoh kodenya."
        )

    if body_text:
        summary = trim_words(body_text, 68)
        return (
            f"Pada bagian {title}, inti pembahasannya adalah sebagai berikut. "
            f"{summary} "
            "Perhatikan bagaimana ide ini terhubung dengan slide sebelumnya, karena pola berpikirnya akan dipakai lagi pada contoh berikutnya."
        )

    return (
        f"Bagian ini memperkenalkan {title}. "
        "Kita gunakan sebagai pengantar sebelum masuk ke rincian konsep dan contoh implementasinya."
    )


def write_template_scripts(
    pdf: Path,
    scripts_dir: Path,
    pdftotext: Path,
    total_pages: int,
    skip_existing: bool,
) -> None:
    for page in range(1, total_pages + 1):
        script_path = scripts_dir / f"slide_{page:02d}.txt"
        if skip_existing and script_path.exists() and script_path.stat().st_size > 0:
            progress(page, total_pages, f"Reusing narration script: {script_path.name}")
            continue
        progress(page, total_pages, f"Writing template narration script: {script_path.name}")
        lines = extract_page_text(pdf, page, total_pages, pdftotext)
        script = script_from_lines(page, lines)
        script_path.write_text(script + "\n", encoding="utf-8")


def collect_slide_texts(pdf: Path, pdftotext: Path, total_pages: int) -> list[dict[str, object]]:
    slides: list[dict[str, object]] = []
    for page in range(1, total_pages + 1):
        progress(page, total_pages, f"Extracting text for Codex script prompt")
        lines = extract_page_text(pdf, page, total_pages, pdftotext)
        slides.append({"slide": page, "text": "\n".join(lines).strip()})
    return slides


def codex_schema(total_pages: int) -> dict[str, object]:
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


def codex_prompt(slides: list[dict[str, object]]) -> str:
    slide_blocks = []
    for slide in slides:
        number = int(slide["slide"])
        text = str(slide["text"]).strip() or "(Tidak ada teks yang berhasil diekstrak.)"
        slide_blocks.append(f"<slide number=\"{number:02d}\">\n{text}\n</slide>")

    return textwrap.dedent(
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
        - Use plain text only in each script. No Markdown, no code fences, no bullet lists.
        - You may add clarifying examples, but do not invent material that
          contradicts the extracted slide text.
        - Output must match the JSON schema exactly.

        Extracted slide text:
        """
    ).strip() + "\n\n" + "\n\n".join(slide_blocks)


def load_codex_response(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def write_codex_scripts(
    pdf: Path,
    scripts_dir: Path,
    work_dir: Path,
    pdftotext: Path,
    total_pages: int,
    skip_existing: bool,
    codex_bin: Path,
    model: str,
    reasoning_effort: str,
    retries: int,
    retry_wait: float,
    timeout: float,
) -> None:
    missing_pages = [
        page
        for page in range(1, total_pages + 1)
        if not (
            skip_existing
            and (scripts_dir / f"slide_{page:02d}.txt").exists()
            and (scripts_dir / f"slide_{page:02d}.txt").stat().st_size > 0
        )
    ]
    if not missing_pages:
        log("All narration scripts already exist; reusing them")
        return

    slides = collect_slide_texts(pdf, pdftotext, total_pages)
    schema_path = work_dir / "codex_scripts_schema.json"
    output_path = work_dir / "codex_scripts_response.json"
    schema_path.write_text(json.dumps(codex_schema(total_pages), indent=2), encoding="utf-8")
    prompt = codex_prompt(slides)

    log(f"Using Codex CLI for script generation: model={model}, reasoning={reasoning_effort}")
    for attempt in range(1, retries + 2):
        if output_path.exists():
            output_path.unlink()

        log(f"Calling Codex CLI for narration scripts (attempt {attempt}/{retries + 1})")
        try:
            run(
                [
                    codex_bin,
                    "exec",
                    "--model",
                    model,
                    "-c",
                    f'model_reasoning_effort="{reasoning_effort}"',
                    "--sandbox",
                    "read-only",
                    "--skip-git-repo-check",
                    "--ephemeral",
                    "--output-schema",
                    schema_path,
                    "--output-last-message",
                    output_path,
                    "-",
                ],
                input_text=prompt,
                capture=True,
                timeout=timeout,
            )
            response = load_codex_response(output_path)
            scripts = response.get("scripts")
            if not isinstance(scripts, list) or len(scripts) != total_pages:
                raise RuntimeError(f"Codex returned {len(scripts) if isinstance(scripts, list) else 'no'} scripts; expected {total_pages}.")

            by_slide: dict[int, str] = {}
            for item in scripts:
                if not isinstance(item, dict):
                    raise RuntimeError("Codex returned a non-object script item.")
                slide = int(item["slide"])
                text = str(item["text"]).strip()
                if not text:
                    raise RuntimeError(f"Codex returned an empty script for slide {slide}.")
                by_slide[slide] = text

            for page in range(1, total_pages + 1):
                script_path = scripts_dir / f"slide_{page:02d}.txt"
                if page not in missing_pages:
                    progress(page, total_pages, f"Reusing narration script: {script_path.name}")
                    continue
                if page not in by_slide:
                    raise RuntimeError(f"Codex response is missing slide {page}.")
                script_path.write_text(by_slide[page] + "\n", encoding="utf-8")
                progress(page, total_pages, f"Codex narration script ready: {script_path.name}")
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError, json.JSONDecodeError) as exc:
            details = command_error_details(exc)
            if attempt > retries:
                message = (
                    f"Codex script generation failed after {retries + 1} attempt(s).\n\n"
                    "You can retry the same command. Add --skip-existing-scripts if some "
                    "script files were already generated successfully.\n\n"
                    f"Last error: {details}"
                )
                raise SystemExit(message + codex_failure_hint(details)) from exc
            log(f"Codex script generation failed; retrying in {retry_wait:.0f}s")
            time.sleep(retry_wait)


def write_scripts(
    provider: str,
    pdf: Path,
    scripts_dir: Path,
    work_dir: Path,
    pdftotext: Path,
    total_pages: int,
    args: argparse.Namespace,
) -> None:
    if provider == "template":
        write_template_scripts(pdf, scripts_dir, pdftotext, total_pages, args.skip_existing_scripts)
        return

    if provider == "codex":
        write_codex_scripts(
            pdf,
            scripts_dir,
            work_dir,
            pdftotext,
            total_pages,
            args.skip_existing_scripts,
            codex_path(args.codex_bin),
            args.codex_model,
            args.codex_reasoning_effort,
            args.codex_retries,
            args.codex_retry_wait,
            args.codex_timeout,
        )
        return

    raise SystemExit(f"Unknown script provider: {provider}")


def audio_duration(path: Path, ffprobe: Path) -> float:
    result = run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        capture=True,
    )
    return float(result.stdout.strip())


def srt_time(seconds: float) -> str:
    millis = int(round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [part.strip() for part in parts if part.strip()]


def audio_metadata_path(audio_path: Path) -> Path:
    return audio_path.with_suffix(audio_path.suffix + ".json")


def script_audio_metadata(script_path: Path, voice: str, rate: str) -> dict[str, object]:
    script_text = script_path.read_text(encoding="utf-8")
    return {
        "schema_version": 1,
        "script_sha256": hashlib.sha256(script_text.encode("utf-8")).hexdigest(),
        "script_bytes": len(script_text.encode("utf-8")),
        "voice": voice,
        "rate": rate,
    }


def audio_matches_script(audio_path: Path, script_path: Path, voice: str, rate: str) -> bool:
    if not audio_path.exists() or audio_path.stat().st_size == 0:
        return False

    metadata_path = audio_metadata_path(audio_path)
    if not metadata_path.exists():
        return False

    try:
        existing = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False

    return existing == script_audio_metadata(script_path, voice, rate)


def write_audio_metadata(audio_path: Path, script_path: Path, voice: str, rate: str) -> None:
    audio_metadata_path(audio_path).write_text(
        json.dumps(script_audio_metadata(script_path, voice, rate), indent=2) + "\n",
        encoding="utf-8",
    )


def write_clip_srt(script_path: Path, duration: float, srt_path: Path) -> None:
    sentences = split_sentences(script_path.read_text(encoding="utf-8"))
    weights = [max(1, len(sentence)) for sentence in sentences]
    total_weight = sum(weights) or 1
    elapsed_weight = 0

    with srt_path.open("w", encoding="utf-8") as handle:
        for index, (sentence, weight) in enumerate(zip(sentences, weights), start=1):
            start = duration * elapsed_weight / total_weight
            elapsed_weight += weight
            end = duration * elapsed_weight / total_weight
            if index == len(sentences):
                end = duration
            handle.write(f"{index}\n{srt_time(start)} --> {srt_time(end)}\n{sentence}\n\n")


def generate_audio(
    edge_tts: Path,
    scripts_dir: Path,
    audio_dir: Path,
    voice: str,
    rate: str,
    total_pages: int,
    skip_existing: bool,
    retries: int,
    retry_wait: float,
    timeout: float,
) -> None:
    for page in range(1, total_pages + 1):
        script = scripts_dir / f"slide_{page:02d}.txt"
        output = audio_dir / f"slide_{page:02d}.mp3"
        if skip_existing and output.exists() and output.stat().st_size > 0:
            if audio_matches_script(output, script, voice, rate):
                progress(page, total_pages, f"Reusing narration audio: {output.name}")
                continue
            progress(page, total_pages, f"Audio is stale or missing metadata; regenerating: {output.name}")

        for attempt in range(1, retries + 2):
            if output.exists():
                output.unlink()
            metadata_path = audio_metadata_path(output)
            if metadata_path.exists():
                metadata_path.unlink()

            attempt_note = f"attempt {attempt}/{retries + 1}"
            progress(page, total_pages, f"Generating narration audio: {output.name} ({attempt_note})")
            try:
                run(
                    [edge_tts, "-f", script, "-v", voice, f"--rate={rate}", "--write-media", output],
                    timeout=timeout,
                )
                if not output.exists() or output.stat().st_size == 0:
                    raise RuntimeError(f"{output} was not created or is empty.")
                write_audio_metadata(output, script, voice, rate)
                break
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError) as exc:
                if output.exists():
                    output.unlink()
                metadata_path = audio_metadata_path(output)
                if metadata_path.exists():
                    metadata_path.unlink()

                if attempt > retries:
                    raise SystemExit(
                        textwrap.dedent(
                            f"""
                            TTS failed for {output.name} after {retries + 1} attempt(s).

                            This is often a temporary DNS/network problem with the online Edge TTS service.
                            You can rerun the same command with --skip-existing-audio so already generated
                            MP3 files are reused and the build resumes from the missing slide.

                            Failed slide: {page}/{total_pages}
                            Last error: {exc}
                            """
                        ).strip()
                    ) from exc

                log(f"TTS failed for {output.name}; retrying in {retry_wait:.0f}s")
                time.sleep(retry_wait)


def resolve_video_settings(args: argparse.Namespace) -> VideoSettings:
    profiles = {
        "lossless": {"crf": 0, "preset": "slow", "pixel_format": "bgr24"},
        "high": {"crf": 12, "preset": "slow", "pixel_format": "yuv444p"},
        "standard": {"crf": 18, "preset": "medium", "pixel_format": "yuv420p"},
    }
    profile = profiles[args.video_quality]
    crf = args.video_crf if args.video_crf is not None else int(profile["crf"])
    if crf < 0 or crf > 51:
        raise SystemExit("--video-crf must be between 0 and 51.")

    pixel_format = args.pixel_format or str(profile["pixel_format"])
    codec = "libx264rgb" if pixel_format in {"bgr24", "rgb24"} else "libx264"

    return VideoSettings(
        quality=args.video_quality,
        codec=codec,
        crf=crf,
        preset=args.video_preset or str(profile["preset"]),
        pixel_format=pixel_format,
    )


def render_clip(
    ffmpeg: Path,
    slide: Path,
    audio: Path,
    clip: Path,
    width: int,
    height: int,
    fps: int,
    video_settings: VideoSettings,
) -> None:
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags={video_settings.scale_flags},"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"setsar=1,format={video_settings.pixel_format}"
    )
    run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-loop",
            "1",
            "-framerate",
            str(fps),
            "-i",
            slide,
            "-i",
            audio,
            "-vf",
            vf,
            "-c:v",
            video_settings.codec,
            "-preset",
            video_settings.preset,
            "-crf",
            str(video_settings.crf),
            "-tune",
            "stillimage",
            "-pix_fmt",
            video_settings.pixel_format,
            "-r",
            str(fps),
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            "-movflags",
            "+faststart",
            clip,
        ]
    )


def parse_srt_time(value: str) -> float:
    hours, minutes, rest = value.split(":")
    seconds, millis = rest.split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


def parse_srt(path: Path) -> list[tuple[float, float, str]]:
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        return []

    cues: list[tuple[float, float, str]] = []
    for block in re.split(r"\n\s*\n", content):
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        start_text, end_text = lines[1].split(" --> ")
        cues.append((parse_srt_time(start_text), parse_srt_time(end_text), "\n".join(lines[2:])))
    return cues


def write_final_srt(subtitles_dir: Path, root_dir: Path, final_name: str, durations: list[float]) -> None:
    final_subtitle = subtitles_dir / f"{final_name}.srt"
    cue_number = 1
    offset = 0.0
    with final_subtitle.open("w", encoding="utf-8") as handle:
        for index, duration in enumerate(durations, start=1):
            clip_srt = subtitles_dir / f"clip_{index:02d}.srt"
            for start, end, text in parse_srt(clip_srt):
                handle.write(f"{cue_number}\n")
                handle.write(f"{srt_time(offset + start)} --> {srt_time(offset + end)}\n")
                handle.write(f"{text}\n\n")
                cue_number += 1
            offset += duration

    (root_dir / f"{final_name}.srt").write_text(final_subtitle.read_text(encoding="utf-8"), encoding="utf-8")


def build_video(args: argparse.Namespace) -> None:
    if not args.pdf or not args.target:
        raise SystemExit("--pdf and --target are required unless --install-only is used.")

    pdf = Path(args.pdf).expanduser().resolve()
    target = Path(args.target).expanduser().resolve()
    if not pdf.exists():
        raise SystemExit(f"PDF not found: {pdf}")

    phase("Startup")
    final_name = args.final_name or pdf.stem
    if args.force:
        clear_generated_outputs(target, final_name)

    tools = require_programs(["pdfinfo", "pdftoppm", "pdftotext", "ffmpeg", "ffprobe"])
    edge_tts = edge_tts_path(args.no_install)
    dirs = prepare_dirs(target)
    video_settings = resolve_video_settings(args)

    log("Reading PDF metadata")
    full_page_count = pdf_page_count(pdf, tools["pdfinfo"])
    total_pages = min(full_page_count, args.max_pages) if args.max_pages else full_page_count

    log(f"PDF: {pdf}")
    log(f"Target: {target}")
    log(f"Pages: {total_pages} of {full_page_count}")
    log(f"Script provider: {args.script_provider}")
    log(f"Voice: {args.voice} at rate {args.rate}")
    log(f"Slide rendering: {args.dpi} DPI")
    log(
        "Video rendering: "
        f"{args.width}x{args.height}@{args.fps}fps, "
        f"quality={video_settings.quality}, codec={video_settings.codec}, "
        f"crf={video_settings.crf}, pixel_format={video_settings.pixel_format}, "
        f"preset={video_settings.preset}"
    )
    if video_settings.quality == "lossless":
        log("Lossless mode keeps slide text and graphics as sharp as possible; expect larger files and slower rendering.")

    phase("Extract Slides")
    render_slide_images(pdf, dirs["slides"], tools["pdftoppm"], args.dpi, total_pages)

    phase("Create Scripts")
    write_scripts(args.script_provider, pdf, dirs["scripts"], dirs["work"], tools["pdftotext"], total_pages, args)

    phase("Generate Audio")
    generate_audio(
        edge_tts,
        dirs["scripts"],
        dirs["audio"],
        args.voice,
        args.rate,
        total_pages,
        args.skip_existing_audio,
        args.tts_retries,
        args.tts_retry_wait,
        args.tts_timeout,
    )

    phase("Render Clips")
    concat_list = dirs["work"] / "concat.txt"
    durations: list[float] = []
    with concat_list.open("w", encoding="utf-8") as concat:
        for page in range(1, total_pages + 1):
            stem = f"slide_{page:02d}"
            clip_stem = f"clip_{page:02d}"
            slide = dirs["slides"] / f"{stem}.png"
            script = dirs["scripts"] / f"{stem}.txt"
            audio = dirs["audio"] / f"{stem}.mp3"
            subtitle = dirs["subtitles"] / f"{clip_stem}.srt"
            clip = dirs["clips"] / f"{clip_stem}.mp4"

            progress(page, total_pages, f"Creating subtitle and clip: {clip_stem}")
            duration = audio_duration(audio, tools["ffprobe"])
            durations.append(math.ceil(duration * args.fps) / args.fps)
            write_clip_srt(script, duration, subtitle)
            render_clip(tools["ffmpeg"], slide, audio, clip, args.width, args.height, args.fps, video_settings)
            concat.write(f"file '{clip.resolve()}'\n")
            progress(page, total_pages, f"Clip ready: {clip.name} ({duration:.1f}s)")

    phase("Merge Final Files")
    final_video = dirs["root"] / f"{final_name}.mp4"
    log(f"Merging {total_pages} clip(s) into {final_video.name}")
    run([tools["ffmpeg"], "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", concat_list, "-c", "copy", final_video])
    log("Merging subtitle timings")
    write_final_srt(dirs["subtitles"], dirs["root"], final_name, durations)

    phase("Done")
    log(f"Final video: {final_video}")
    log(f"Final subtitles: {dirs['root'] / f'{final_name}.srt'}")


def main() -> None:
    global COMMAND_VERBOSE
    args = parse_args()
    COMMAND_VERBOSE = args.verbose
    if args.install_only:
        phase("Install Dependencies")
        install_python_deps()
        log(f"Installed Python dependencies into {VENV_DIR}")
        return
    build_video(args)


if __name__ == "__main__":
    main()
