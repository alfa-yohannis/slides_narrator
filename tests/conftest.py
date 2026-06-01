"""Shared pytest fixtures/helpers for the Slides Narrator test suite.

The build script (`slides_narrator/build.py`) does two things at import time that
we have to neutralise before we can import it as a plain module:

1. `_bootstrap_venv()` — re-execs into a local `.venv`. We skip it by setting the
   marker env var the bootstrap checks for.
2. `import edge_tts` — a third-party dep. If it isn't importable in the current
   interpreter (e.g. running unit tests under the system python) we install a
   minimal stub so the import succeeds; no unit test calls into it.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import struct
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_PATH = REPO_ROOT / "slides_narrator" / "build.py"


def _load_build_module():
    # 1. Skip the venv bootstrap.
    os.environ.setdefault("SLIDES_NARRATOR_VENV", "1")
    # 2. Make `import edge_tts` succeed even without the real package.
    if "edge_tts" not in sys.modules:
        try:
            import edge_tts  # noqa: F401
        except Exception:
            stub = types.ModuleType("edge_tts")
            stub.Communicate = type("Communicate", (), {})
            stub.SubMaker = type("SubMaker", (), {})
            sys.modules["edge_tts"] = stub

    spec = importlib.util.spec_from_file_location("slides_narrator_build", BUILD_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


# Imported once; build.py is side-effect free past the (skipped) bootstrap.
build = _load_build_module()


@pytest.fixture()
def b():
    """The loaded build module under test."""
    return build


# --- helpers ---------------------------------------------------------------


def have(*binaries: str) -> bool:
    return all(shutil.which(x) for x in binaries)


def requires(*binaries: str):
    """Skip marker for integration tests that need external tools."""
    missing = [x for x in binaries if not shutil.which(x)]
    return pytest.mark.skipif(bool(missing), reason=f"missing tools: {', '.join(missing)}")


def make_test_pdf(path: Path, texts: list[str]) -> Path:
    """Write a minimal, valid multi-page PDF (one short text line per page).

    Hand-built so the suite needs no PDF library. Each page is US-Letter with a
    single Helvetica line, which is enough for pdfinfo/pdftoppm/pdftotext.
    """
    objects: list[bytes] = []

    def add(obj: bytes) -> int:
        objects.append(obj)
        return len(objects)  # 1-based object number

    n = len(texts)
    # Reserve object numbers: 1=Catalog, 2=Pages, then per page a Page + Contents.
    catalog_num = 1
    pages_num = 2
    page_nums = []
    content_nums = []
    font_num = 2 + 2 * n + 1  # after pages + (page,content)*n

    # Pre-compute page object numbers.
    next_num = 3
    for _ in range(n):
        page_nums.append(next_num)
        content_nums.append(next_num + 1)
        next_num += 2

    # Build objects in numeric order.
    add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_num)  # 1
    kids = " ".join(f"{p} 0 R" for p in page_nums)
    add(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids.encode(), n))  # 2
    for i in range(n):
        page = (
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages_num, font_num, content_nums[i])
        )
        text = texts[i].replace("(", r"\(").replace(")", r"\)")
        stream = b"BT /F1 24 Tf 72 700 Td (%s) Tj ET" % text.encode("latin-1", "replace")
        content = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)
        add(page)
        add(content)
    add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")  # font

    # Serialise with an xref table.
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]  # object 0 is the free head
    for num, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % num
        out += body
        out += b"\nendobj\n"

    xref_pos = len(out)
    count = len(objects) + 1
    out += b"xref\n0 %d\n" % count
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\n" % (count, catalog_num)
    out += b"startxref\n%d\n%%%%EOF" % xref_pos

    path.write_bytes(bytes(out))
    return path


def silent_mp3(path: Path, seconds: float = 1.0) -> Path:
    """Generate a silent MP3 via ffmpeg (for clip-encoding integration tests)."""
    import subprocess

    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "anullsrc=r=24000:cl=mono", "-t", str(seconds),
         "-c:a", "libmp3lame", "-q:a", "9", str(path)],
        check=True,
    )
    return path


def pcm_silence(seconds: float = 1.0, rate: int = 24000) -> bytes:
    return struct.pack("<%dh" % int(rate * seconds), *([0] * int(rate * seconds)))
