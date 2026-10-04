#!/usr/bin/env python3
"""Generate minimal, dependency-free glTF fixtures for the 3D-preview e2e
tests: a box 20 mm wide, 30 mm deep and 40 mm tall.

glTF is Y-up and in meters, so the box is 0.02 (X) by 0.04 (Y, up) by
0.03 (Z). The viewer rotates it onto +Z-up and scales to mm, so the dims pill
must read 20 x 30 x 40: that checks the orientation as well as the scale.
Run from anywhere:

    uv run tests/fixtures/make_sample_gltf.py

Writes next to this script:
- `box_20x30x40.glb`: binary glTF, buffer embedded.
- `box_20x30x40.gltf` + `box_20x30x40.bin`: JSON glTF with an external
  buffer, which exercises the relative-URI resolution.

Deterministic: re-running produces byte-identical output.
"""

import json
import struct
from pathlib import Path

W, H, D = 0.02, 0.04, 0.03  # glTF X, Y (up), Z


def _buffer() -> tuple[bytes, int, int]:
    x, y, z = W / 2, H / 2, D / 2
    corners = [
        (-x, -y, -z),
        (x, -y, -z),
        (x, y, -z),
        (-x, y, -z),
        (-x, -y, z),
        (x, -y, z),
        (x, y, z),
        (-x, y, z),
    ]
    # Two counter-clockwise triangles per face, viewed from outside
    faces = [
        # -Z
        (0, 2, 1),
        (0, 3, 2),
        # +Z
        (4, 5, 6),
        (4, 6, 7),
        # -Y
        (0, 1, 5),
        (0, 5, 4),
        # +Y
        (3, 7, 6),
        (3, 6, 2),
        # -X
        (0, 4, 7),
        (0, 7, 3),
        # +X
        (1, 2, 6),
        (1, 6, 5),
    ]
    positions = b"".join(struct.pack("<3f", *c) for c in corners)
    indices = b"".join(struct.pack("<3H", *f) for f in faces)
    return positions + indices, len(positions), len(indices)


def _document(buffer_len: int, pos_len: int, idx_len: int, uri: str | None) -> dict:
    buffer = {"byteLength": buffer_len}
    if uri:
        buffer["uri"] = uri
    return {
        "asset": {"version": "2.0", "generator": "merlin make_sample_gltf.py"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [
            {
                "primitives": [
                    {"attributes": {"POSITION": 0}, "indices": 1, "material": 0}
                ]
            }
        ],
        "materials": [
            {
                "pbrMetallicRoughness": {
                    "baseColorFactor": [0.2, 0.6, 0.3, 1.0],
                    "metallicFactor": 0.0,
                    "roughnessFactor": 0.7,
                }
            }
        ],
        "buffers": [buffer],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": pos_len, "target": 34962},
            {
                "buffer": 0,
                "byteOffset": pos_len,
                "byteLength": idx_len,
                "target": 34963,
            },
        ],
        "accessors": [
            {
                "bufferView": 0,
                "componentType": 5126,
                "count": 8,
                "type": "VEC3",
                "min": [-W / 2, -H / 2, -D / 2],
                "max": [W / 2, H / 2, D / 2],
            },
            {"bufferView": 1, "componentType": 5123, "count": 36, "type": "SCALAR"},
        ],
    }


def _pad(data: bytes, fill: bytes) -> bytes:
    return data + fill * (-len(data) % 4)


def build_glb() -> bytes:
    buf, pos_len, idx_len = _buffer()
    buf = _pad(buf, b"\x00")
    doc = json.dumps(_document(len(buf), pos_len, idx_len, None), separators=(",", ":"))
    json_chunk = _pad(doc.encode(), b" ")
    total = 12 + 8 + len(json_chunk) + 8 + len(buf)
    return (
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<I4s", len(json_chunk), b"JSON")
        + json_chunk
        + struct.pack("<I4s", len(buf), b"BIN\x00")
        + buf
    )


def build_gltf(bin_name: str) -> tuple[str, bytes]:
    buf, pos_len, idx_len = _buffer()
    doc = _document(len(buf), pos_len, idx_len, bin_name)
    return json.dumps(doc, indent=2) + "\n", buf


if __name__ == "__main__":
    here = Path(__file__).parent
    (here / "box_20x30x40.glb").write_bytes(build_glb())
    text, buf = build_gltf("box_20x30x40.bin")
    (here / "box_20x30x40.gltf").write_text(text)
    (here / "box_20x30x40.bin").write_bytes(buf)
    print("Wrote box_20x30x40.glb, box_20x30x40.gltf, box_20x30x40.bin")
