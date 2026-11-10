"""Hostile and broken input: every failure is a clear MvtDecodeError/TileSourceError, never a crash or a hang."""

from __future__ import annotations

import contextlib
import gzip
import random
import threading
import time
import zlib
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import shapely
from conftest import FIXTURES
from mvt_encode import F, geometry, key, layer, ld, points, tile, varint

from mvt_worker import sources
from mvt_worker.mvt import (
    COORDINATE_CHOICES,
    MAX_TILE_BYTES,
    MvtDecodeError,
    Projector,
    decode_tile,
    feature_wkb,
    maybe_decompress,
    tile_context,
)
from mvt_worker.rows import RowBuilder, output_schema

SEEDS = [
    maybe_decompress((FIXTURES / "0-0-0.mvt").read_bytes()),
    tile(
        layer(
            "l",
            [
                F(1, points([(1, 2), (3, 4)]), {"s": "x", "f": 1.5, "i": -3, "u": 7, "b": True}, id=1),
                F(2, geometry([[(0, 0), (10, 10), (20, 0)], [(5, 5), (6, 6)]], close=False), id=2),
                F(3, geometry([[(0, 0), (100, 0), (100, 100), (0, 0)]], close=True), {"k": "v"}),
            ],
        ),
        layer("old", [F(3, geometry([[(0, 0), (0, 9), (9, 9), (0, 0)]], close=True))], version=1),
    ),
]


def _decode_all(data: bytes, coordinates: str = "wgs84", z: int = 0) -> int:
    n = 0
    for lay in decode_tile(data):
        project = Projector(z, 0, 0, lay.extent, coordinates)  # type: ignore[arg-type]
        for f in lay.features:
            feature_wkb(f, project, lay.version)
            n += 1
    return n


def _mutate(rng: random.Random, b: bytearray) -> bytes:
    for _ in range(rng.randint(1, 6)):
        if not b:
            b = bytearray(rng.randbytes(4))
        i = rng.randrange(len(b))
        op = rng.random()
        if op < 0.4:
            b[i] ^= 1 << rng.randrange(8)
        elif op < 0.55:
            b[i] = rng.choice([0x00, 0x7F, 0x80, 0xFF, 0x0A, 0x12, 0x1A, 0x22, 0x28])
        elif op < 0.7:
            del b[i : i + rng.randint(1, 16)]
        elif op < 0.85:
            b[i:i] = rng.randbytes(rng.randint(1, 16))
        else:
            b[i:i] = b"\xff" * rng.randint(1, 11)  # over-long varints
    return bytes(b)


def test_mutated_tiles_raise_only_decode_errors() -> None:
    """A seeded mutation fuzz; scripts/conformance.py runs a longer one."""
    rng = random.Random(1234)
    for _ in range(3000):
        data = _mutate(rng, bytearray(rng.choice(SEEDS)))
        start = time.perf_counter()
        with contextlib.suppress(MvtDecodeError):
            _decode_all(data, rng.choice(COORDINATE_CHOICES), rng.randint(0, 22))
        assert time.perf_counter() - start < 1.0


def test_mutated_gzip_raises_only_decode_errors() -> None:
    rng = random.Random(99)
    packed = gzip.compress(SEEDS[1])
    for _ in range(1000):
        b = bytearray(packed)
        for _ in range(rng.randint(1, 3)):
            b[rng.randrange(len(b))] ^= 1 << rng.randrange(8)
        with contextlib.suppress(MvtDecodeError):
            _decode_all(bytes(b))


def test_wrong_wire_type_for_a_float_value() -> None:
    # Value.float_value (field 2) must be fixed32; a length-delimited field there crashed struct.unpack.
    bad_float = ld(2, b"\x01\x02")
    bad_double = ld(3, b"\x01")
    body = ld(1, b"l") + ld(2, key(2, 2) + varint(2) + b"\x00\x00" + key(3, 0) + varint(1) + ld(4, b"\x09\x02\x02"))
    body += ld(3, b"a") + ld(4, bad_float) + ld(4, bad_double) + key(15, 0) + varint(2)
    [lay] = decode_tile(ld(3, body))
    assert lay.features[0].properties == {"a": None}


def test_decompression_bomb_is_refused() -> None:
    bomb = gzip.compress(b"\0" * (MAX_TILE_BYTES + 1), 9)
    assert len(bomb) < 100_000
    with pytest.raises(MvtDecodeError, match="expands past 64 MiB"):
        maybe_decompress(bomb)
    with pytest.raises(MvtDecodeError, match="expands past"):
        maybe_decompress(zlib.compress(b"\0" * (MAX_TILE_BYTES + 1), 9))


def test_damaged_and_concatenated_gzip() -> None:
    raw = SEEDS[1]
    packed = gzip.compress(raw)
    with pytest.raises(MvtDecodeError, match="truncated gzip"):
        maybe_decompress(packed[:-6])
    with pytest.raises(MvtDecodeError, match="corrupt gzip"):
        maybe_decompress(packed[:-8] + b"\0" * 8)
    assert maybe_decompress(packed + packed) == raw + raw  # multi-member gzip
    assert maybe_decompress(zlib.compress(raw)) == raw
    assert maybe_decompress(raw) == raw


def test_errors_name_the_tile() -> None:
    rows = RowBuilder(output_schema(), "wgs84", None)
    with pytest.raises(MvtDecodeError, match=r"^tile 3/2/1 is not a valid vector tile: truncated"):
        rows.add_tile(3, 2, 1, b"\x1a\x7f")
    with pytest.raises(MvtDecodeError, match=r"^tile 1/0/0 .*gzip"), tile_context(1, 0, 0):
        maybe_decompress(b"\x1f\x8b\x08\x00garbage")


def test_huge_coordinates_project_without_overflow() -> None:
    # mvt-fixtures 049/050: valid tiles with coordinates past int32.
    f = F(2, geometry([[(0, -(2**31)), (-1, -(2**31) - 1)], [(2**31 - 1, 0), (2**31, 1)]], close=False))
    data = tile(layer("l", [f]))
    for coordinates in COORDINATE_CHOICES:
        [lay] = decode_tile(data)
        _gtype, wkb = feature_wkb(lay.features[0], Projector(0, 0, 0, 4096, coordinates), 2)
        assert wkb is not None
        assert not shapely.from_wkb(wkb).is_empty


def test_ids_wrap_to_64_bits() -> None:
    fb = key(1, 0) + b"\xff" * 9 + b"\x7f" + key(3, 0) + varint(1) + ld(4, varint(9) + varint(2) + varint(2))
    [lay] = decode_tile(ld(3, ld(1, b"l") + ld(2, fb)))
    assert lay.features[0].id == 2**64 - 1


def test_huge_command_counts_do_not_allocate() -> None:
    f = F(1, [(1 | (2**28 << 3)), 2, 2])  # MoveTo claiming 2^28 points, carrying one
    [lay] = decode_tile(tile(layer("l", [f])))
    start = time.perf_counter()
    gtype, _ = feature_wkb(lay.features[0], Projector(0, 0, 0, 4096, "tile"), 2)
    assert gtype == "POINT"
    assert time.perf_counter() - start < 0.5


# ---------------------------------------------------------------- HTTP


class _NoRangeHandler(BaseHTTPRequestHandler):
    """Serves one file and ignores Range, like some static hosts."""

    body = b""

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        pass


@pytest.fixture
def no_range_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NoRangeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/x"
    finally:
        server.shutdown()


def test_server_ignoring_range_is_sliced_or_refused(no_range_server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _NoRangeHandler.body = bytes(range(256)) * 64
    assert sources.http_get(no_range_server, (10, 5)) == bytes(range(10, 15))
    monkeypatch.setattr(sources, "MAX_HTTP_BYTES", 1000)
    with pytest.raises(sources.TileSourceError, match="does not support HTTP range"):
        sources.http_get(no_range_server, (5000, 10))
    with pytest.raises(sources.TileSourceError, match="larger than"):
        sources.http_get(no_range_server)


def test_oversized_local_tile_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "big.mvt"
    p.write_bytes(b"\0" * 2000)
    monkeypatch.setattr(sources, "MAX_HTTP_BYTES", 1000)
    with pytest.raises(sources.TileSourceError, match="not a single vector tile"):
        sources.read_bytes(str(p))


class _KeepAliveRangeHandler(BaseHTTPRequestHandler):
    """HTTP/1.1 keep-alive with single byte ranges over test/fixtures; counts requests and connections."""

    protocol_version = "HTTP/1.1"
    requests = 0
    connections: set[tuple[str, int]] = set()

    def do_GET(self) -> None:
        type(self).requests += 1
        type(self).connections.add(self.client_address)
        data = (FIXTURES / self.path.lstrip("/")).read_bytes()
        start_s, _, end_s = self.headers.get("Range", "bytes=0-").removeprefix("bytes=").partition("-")
        start, end = int(start_s), min(int(end_s) if end_s else len(data) - 1, len(data) - 1)
        body = data[start : end + 1]
        self.send_response(206 if "Range" in self.headers else 200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        pass


def test_remote_pmtiles_coalesces_reads_on_kept_alive_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sources, "HEAD_BYTES", 127)  # header only: directories and tiles need requests
    server = ThreadingHTTPServer(("127.0.0.1", 0), _KeepAliveRangeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/edges.pmtiles"
        local = sources.PMTilesSource(str(FIXTURES / "edges.pmtiles"))
        remote = sources.PMTilesSource(url)
        keys = remote.tiles(3, None, 100)  # reads the root directory
        assert keys == local.tiles(3, None, 100)
        _KeepAliveRangeHandler.requests = 0
        assert remote.fetch(keys) == local.fetch(keys)
        assert len(keys) > 1
        assert _KeepAliveRangeHandler.requests == 1  # neighbouring tiles: one range request
        assert len(_KeepAliveRangeHandler.connections) == 1  # header, directory and tiles share a socket
    finally:
        server.shutdown()


def test_coalesce_spans() -> None:
    gap = sources.COALESCE_GAP
    spans = sources._coalesce([(0, 10), (10, 5), (20 + gap, 5), (20 + gap, 5), (10**9, 1)])
    assert [(s, n) for s, n, _ in spans] == [(0, 15), (20 + gap, 5), (10**9, 1)]
    assert spans[0][2] == [(0, 10), (10, 5)]
