"""Decoder conformance check: run before a release (`make conformance`).

1. Mapbox's mvt-fixtures suite (downloaded from npm into .cache/, not committed:
   the fixtures are ODbL). Every fixture must decode without an unexpected error,
   and every valid one must match mapbox-vector-tile, an independent decoder,
   feature by feature: id, properties (value and type) and exact geometry.
2. The suite's real-world Mapbox Streets tiles (~390k features), compared the same way.
3. Optionally, any tilesets passed as arguments (`SOURCE ZOOM [MAX_TILES]`), e.g.
   a PMTiles URL; tiles are sampled at random with a fixed seed.
4. A mutation fuzz of the decoder: only MvtDecodeError may escape, and no tile may
   take more than a second.

Usage: uv run --with mapbox-vector-tile python scripts/conformance.py [--fuzz N] [SOURCE ZOOM [MAX_TILES]] ...
"""

from __future__ import annotations

import argparse
import io
import json
import random
import re
import sys
import tarfile
import time
import urllib.request
from collections import Counter
from pathlib import Path

import mapbox_vector_tile
import shapely
from shapely.geometry import shape

from mvt_worker.mvt import (
    COORDINATE_CHOICES,
    MvtDecodeError,
    Projector,
    decode_tile,
    feature_wkb,
    maybe_decompress,
)
from mvt_worker.sources import open_source

FIXTURES_URL = "https://registry.npmjs.org/@mapbox/mvt-fixtures/-/mvt-fixtures-4.0.0.tgz"
CACHE = Path(__file__).resolve().parent.parent / ".cache" / "mvt-fixtures-4.0.0"


def fetch_fixtures() -> Path:
    root = CACHE / "package"
    if not root.is_dir():
        print(f"downloading {FIXTURES_URL}")
        with urllib.request.urlopen(FIXTURES_URL, timeout=60) as resp:
            data = resp.read()
        CACHE.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            tar.extractall(CACHE, filter="data")
    return root


class Diff:
    """Compare our decode of a tile with mapbox-vector-tile's."""

    def __init__(self) -> None:
        self.stats: Counter[str] = Counter()
        self.bad: list[str] = []

    def fail(self, msg: str) -> None:
        self.stats["mismatch"] += 1
        if len(self.bad) < 30:
            self.bad.append(msg)

    def tile(self, name: str, key: tuple[int, int, int], data: bytes) -> None:
        raw = maybe_decompress(data)
        ours = decode_tile(raw)
        try:
            theirs = mapbox_vector_tile.decode(raw, default_options={"y_coord_down": True})
        except (ValueError, IndexError):  # it rejects UNKNOWN types and some odd command streams
            self.stats["reference_cannot_read"] += 1
            return
        self.stats["tiles"] += 1
        if sorted({lay.name for lay in ours}) != sorted(theirs):
            self.fail(f"{name}: layers {[lay.name for lay in ours]} vs {list(theirs)}")
            return
        for lay in ours:
            want = theirs[lay.name]["features"]
            if len(want) != len(lay.features):
                if [x.name for x in ours].count(lay.name) == 1:  # duplicate names merge over there
                    self.fail(f"{name}/{lay.name}: {len(lay.features)} vs {len(want)} features")
                continue
            project = Projector(*key, lay.extent, "tile")
            for i, (f, g) in enumerate(zip(lay.features, want, strict=True)):
                self.stats["features"] += 1
                where = f"{name}/{lay.name}#{i}"
                if (f.id or 0) != (g.get("id") or 0):  # they report a missing id as 0
                    self.fail(f"{where}: id {f.id} vs {g.get('id')}")
                p, q = f.properties, g["properties"]
                if p != q or any(type(p[k]) is not type(q.get(k)) for k in p):
                    self.fail(f"{where}: properties {p} vs {q}")
                _gtype, wkb = feature_wkb(f, project, lay.version)
                tg = g.get("geometry") or {}
                if wkb is None or not tg.get("coordinates"):
                    if (wkb is None) != (not tg.get("coordinates")):
                        self.fail(f"{where}: empty geometry {wkb is None} vs {tg}")
                    continue
                a, b = shapely.from_wkb(wkb), shape(tg)
                if a.geom_type != b.geom_type or not shapely.equals_exact(
                    shapely.normalize(a), shapely.normalize(b), tolerance=0
                ):
                    self.fail(f"{where}: geometry {a.wkt[:70]} vs {b.wkt[:70]}")

    def report(self, label: str) -> bool:
        print(f"{label}: {dict(self.stats)}")
        for b in self.bad:
            print("   ", b)
        return not self.stats["mismatch"]


def spec_fixtures(root: Path) -> bool:
    ok = True
    diff = Diff()
    for d in sorted((root / "fixtures").iterdir()):
        info = json.loads((d / "info.json").read_text())
        valid = info["validity"]["v2"] or info["validity"]["v1"]
        data = (d / "tile.mvt").read_bytes()
        try:
            n = sum(len(lay.features) for lay in decode_tile(data))
            for lay in decode_tile(data):
                for f in lay.features:
                    for c in COORDINATE_CHOICES:
                        feature_wkb(f, Projector(0, 0, 0, lay.extent, c), lay.version)
        except MvtDecodeError as exc:
            if valid:
                print(f"    fixture {d.name} is valid but failed: {exc}")
                ok = False
            continue
        except Exception as exc:
            print(f"    fixture {d.name} raised {type(exc).__name__}: {exc}")
            ok = False
            continue
        if valid and n:
            diff.tile(f"fixture {d.name}", (0, 0, 0), data)
    return diff.report("mvt-fixtures spec suite (74 tiles)") and ok


def real_world(root: Path) -> bool:
    diff = Diff()
    for p in sorted((root / "real-world").rglob("*.mvt*")):
        m = re.match(r"(\d+)-(\d+)-(\d+)", p.name)
        key = (int(m[1]), int(m[2]), int(m[3])) if m else (0, 0, 0)
        diff.tile(f"{p.parent.name}/{p.name}", key, p.read_bytes())
    return diff.report("mvt-fixtures real-world tiles")


def tileset(source: str, zoom: int, limit: int) -> bool:
    src = open_source(source)
    keys = src.tiles(zoom, None, 10_000_000)
    rng = random.Random(1)
    if len(keys) > limit:
        keys = rng.sample(keys, limit)
    diff = Diff()
    for key, data in src.fetch(keys):
        diff.tile(f"{key}", key, data)
    return diff.report(f"{source} z{zoom}")


def fuzz(root: Path, n: int) -> bool:
    seeds = [maybe_decompress(p.read_bytes()) for p in sorted((root / "fixtures").glob("0[1-6]*/tile.mvt"))]
    seeds += [maybe_decompress(p.read_bytes()) for p in sorted((root / "real-world").rglob("*.mvt*"))[:8]]
    rng = random.Random(7)
    other: Counter[str] = Counter()
    slowest = 0.0
    for _ in range(n):
        b = bytearray(rng.choice(seeds))
        for _ in range(rng.randint(1, 8)):
            if not b:
                b = bytearray(rng.randbytes(8))
            i = rng.randrange(len(b))
            op = rng.random()
            if op < 0.45:
                b[i] ^= 1 << rng.randrange(8)
            elif op < 0.6:
                del b[i : i + rng.randint(1, 32)]
            elif op < 0.8:
                b[i:i] = rng.randbytes(rng.randint(1, 16))
            elif op < 0.9:
                b[i:i] = b"\xff" * rng.randint(1, 11)
            else:
                b = b[: rng.randrange(len(b) + 1)]
        start = time.perf_counter()
        try:
            for lay in decode_tile(bytes(b)):
                project = Projector(rng.randint(0, 22), 0, 0, lay.extent, rng.choice(COORDINATE_CHOICES))
                for f in lay.features:
                    feature_wkb(f, project, lay.version)
        except MvtDecodeError:
            pass
        except Exception as exc:
            other[f"{type(exc).__name__}: {exc}"[:100]] += 1
        slowest = max(slowest, time.perf_counter() - start)
    print(f"fuzz: {n} mutated tiles, unexpected errors {dict(other)}, slowest {slowest:.3f}s")
    return not other and slowest < 1.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fuzz", type=int, default=20000, help="mutated tiles to decode (default 20000)")
    ap.add_argument("tilesets", nargs="*", help="SOURCE ZOOM [MAX_TILES] groups")
    args = ap.parse_args()
    root = fetch_fixtures()
    ok = spec_fixtures(root)
    ok = real_world(root) and ok
    rest = list(args.tilesets)
    while rest:
        source, zoom = rest[0], int(rest[1])
        limit = int(rest[2]) if len(rest) > 2 and rest[2].isdigit() else 200
        rest = rest[3:] if len(rest) > 2 and rest[2].isdigit() else rest[2:]
        ok = tileset(source, zoom, limit) and ok
    ok = fuzz(root, args.fuzz) and ok
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
