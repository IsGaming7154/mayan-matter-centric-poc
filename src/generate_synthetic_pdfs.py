"""Generate 3 tiny synthetic PDFs for the matter-tree test.

Pure-Python -writes minimal valid single-page PDFs without any dependency.
No real client data. Filenames map to the matter the doc will be tagged with.

Outputs:
  ../samples/acme-2025-001-correspondence.pdf
  ../samples/acme-2025-001-pleadings.pdf
  ../samples/wayne-2025-007-correspondence.pdf
"""

from __future__ import annotations

from pathlib import Path

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"


def write_pdf(path: Path, text: str) -> None:
    # Minimal 1-page PDF. Single Type1 Helvetica font, single text-show op.
    body = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj\n"
        b"4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    )
    stream = f"BT /F1 16 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    body += (
        f"5 0 obj<</Length {len(stream)}>>stream\n".encode("ascii")
        + stream
        + b"\nendstream endobj\n"
    )
    # xref + trailer with cumulative byte offsets.
    offsets = [0]
    pos = 0
    for line in body.split(b"\n"):
        if line.startswith(b"1 0 obj") and 1 not in offsets:
            offsets.append(pos)
        if line.startswith(b"2 0 obj"):
            offsets.append(pos)
        if line.startswith(b"3 0 obj"):
            offsets.append(pos)
        if line.startswith(b"4 0 obj"):
            offsets.append(pos)
        if line.startswith(b"5 0 obj"):
            offsets.append(pos)
        pos += len(line) + 1
    xref_pos = len(body)
    xref = b"xref\n0 6\n0000000000 65535 f \n"
    for off in offsets[1:]:
        xref += f"{off:010d} 00000 n \n".encode("ascii")
    trailer = (
        f"trailer<</Size 6/Root 1 0 R>>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii")
    )
    path.write_bytes(body + xref + trailer)


def main() -> int:
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    docs = [
        ("acme-2025-001-correspondence.pdf", "Acme matter 2025-001 -correspondence sample"),
        ("acme-2025-001-pleadings.pdf", "Acme matter 2025-001 -pleadings sample"),
        ("wayne-2025-007-correspondence.pdf", "Wayne matter 2025-007 -correspondence sample"),
    ]
    for filename, text in docs:
        out = SAMPLES_DIR / filename
        write_pdf(out, text)
        print(f"  wrote {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
