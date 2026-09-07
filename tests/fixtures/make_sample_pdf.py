#!/usr/bin/env python3
"""Generate a minimal, dependency-free 2-page PDF fixture for the PDF-preview
e2e tests.

Each page draws a filled black rectangle (guarantees non-uniform canvas pixels
for the "not blank" assertion) plus a text label. Run from anywhere:

    uv run tests/fixtures/make_sample_pdf.py

Writes `sample_2page.pdf` next to this script. Deterministic: re-running
produces byte-identical output, so the committed fixture stays reproducible.
"""

from pathlib import Path


def _page_content(label: str) -> bytes:
    return (
        b"0 0 0 rg\n"  # black fill
        b"80 300 300 260 re f\n"  # filled rectangle -> non-blank pixels
        b"BT /F1 36 Tf 90 620 Td (" + label.encode("ascii") + b") Tj ET\n"
    )


def build_pdf() -> bytes:
    # Object bodies (object 0 is the free head, added by the xref writer).
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 7 0 R >> >> /Contents 4 0 R >>",
        None,  # 4: page 1 content stream (filled below)
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 7 0 R >> >> /Contents 6 0 R >>",
        None,  # 6: page 2 content stream (filled below)
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    def stream_obj(content: bytes) -> bytes:
        return (
            b"<< /Length " + str(len(content)).encode("ascii") + b" >>\n"
            b"stream\n" + content + b"\nendstream"
        )

    objects[3] = stream_obj(_page_content("Page 1"))
    objects[5] = stream_obj(_page_content("Page 2"))

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0] * (len(objects) + 1)  # index 0 = free object
    for i, body in enumerate(objects, start=1):
        offsets[i] = len(out)
        out += str(i).encode("ascii") + b" 0 obj\n" + body + b"\nendobj\n"

    xref_pos = len(out)
    n = len(objects) + 1
    out += b"xref\n"
    out += b"0 " + str(n).encode("ascii") + b"\n"
    out += b"0000000000 65535 f \n"
    for i in range(1, n):
        out += ("%010d 00000 n \n" % offsets[i]).encode("ascii")
    out += (
        b"trailer\n<< /Size " + str(n).encode("ascii") + b" /Root 1 0 R >>\n"
        b"startxref\n" + str(xref_pos).encode("ascii") + b"\n%%EOF\n"
    )
    return bytes(out)


if __name__ == "__main__":
    dest = Path(__file__).parent / "sample_2page.pdf"
    dest.write_bytes(build_pdf())
    print(f"Wrote {dest} ({dest.stat().st_size} bytes)")
