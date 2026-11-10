"""Where tiles come from: PMTiles, MBTiles, XYZ URL templates and single tile files.

Every source answers three questions: what is its metadata (``vector_layers``,
zoom range, bounds), which tiles exist at a zoom (optionally inside a bbox), and
what are the bytes of tile ``z/x/y``. Local files and ``http(s)://`` URLs both
work; PMTiles over HTTP uses range requests, so a multi-gigabyte archive costs
only the directory pages and tiles you actually read.
"""

from __future__ import annotations

import http.client
import json
import logging
import mmap
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pmtiles.tile import (
    Compression,
    Entry,
    TileType,
    deserialize_header,
    find_tile,
    tileid_to_zxy,
    zxy_to_tileid,
)

from .mvt import MAX_TILE_BYTES, MvtDecodeError, maybe_decompress, read_varint, tile_context, tile_range

log = logging.getLogger(__name__)

USER_AGENT = "vgi-mvt/0.1 (+https://github.com/Query-farm/vgi-mvt)"
HTTP_TIMEOUT = 30.0
HTTP_RETRIES = 4
FETCH_THREADS = 8
DIRECTORY_CACHE = 256  # parsed PMTiles directories kept per open archive
HEAD_BYTES = 16384  # first read of a PMTiles archive: header + root directory (+ metadata, usually)
# Largest body read from one URL: a whole .mvt or a PMTiles directory/tile range.
MAX_HTTP_BYTES = 2 * MAX_TILE_BYTES

TileKey = tuple[int, int, int]
BBox = tuple[float, float, float, float]  # west, south, east, north


class TileSourceError(ValueError):
    """The source cannot be opened or read."""


def is_url(source: str) -> bool:
    """Return True for http:// and https:// sources."""
    return source.startswith(("http://", "https://"))


# ---------------------------------------------------------------- HTTP


class _HttpStatusError(Exception):
    def __init__(self, code: int, retry_after: str | None) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


_LOCAL = threading.local()
_SSL = ssl.create_default_context()


def _connection(scheme: str, netloc: str) -> http.client.HTTPConnection:
    """Return this thread's kept-alive connection to a host, so range reads skip the TLS handshake."""
    conns: dict[tuple[str, str], http.client.HTTPConnection] = _LOCAL.__dict__.setdefault("conns", {})
    conn = conns.get((scheme, netloc))
    if conn is None:
        if scheme == "https":
            conn = http.client.HTTPSConnection(netloc, timeout=HTTP_TIMEOUT, context=_SSL)
        else:
            conn = http.client.HTTPConnection(netloc, timeout=HTTP_TIMEOUT)
        conns[(scheme, netloc)] = conn
    return conn


def _drop_connection(scheme: str, netloc: str) -> None:
    conn = _LOCAL.__dict__.get("conns", {}).pop((scheme, netloc), None)
    if conn is not None:
        conn.close()


def _send(scheme: str, netloc: str, path: str, headers: dict[str, str]) -> http.client.HTTPResponse:
    """Send a GET on the pooled connection; a failed connection never stays in the pool."""
    for fresh in (False, True):
        conn = _connection(scheme, netloc)
        try:
            conn.request("GET", path, headers=headers)
            return conn.getresponse()
        except (http.client.HTTPException, OSError):
            _drop_connection(scheme, netloc)
            if fresh:  # the first failure may just be a stale keep-alive socket; a second is real
                raise
    raise AssertionError("unreachable")  # pragma: no cover


def _get_once(url: str, headers: dict[str, str], byte_range: tuple[int, int] | None) -> bytes | None:
    """One GET (following up to 5 redirects); raises _HttpStatusError for retryable/error statuses."""
    if urllib.request.getproxies():  # honour HTTP(S)_PROXY: let urllib route the request
        return _get_urllib(url, headers, byte_range)
    for _hop in range(6):
        parts = urllib.parse.urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        resp = _send(parts.scheme, parts.netloc, path, headers)
        try:
            if resp.status in (301, 302, 303, 307, 308) and resp.getheader("Location"):
                resp.read()
                url = urllib.parse.urljoin(url, resp.getheader("Location", ""))
                continue
            if resp.status in (204, 404):
                resp.read()
                return None
            if resp.status >= 400:
                resp.read()
                raise _HttpStatusError(resp.status, resp.getheader("Retry-After"))
            body, complete = _body(resp, url, byte_range)
            if not complete or resp.getheader("Connection", "").lower() == "close":
                _drop_connection(parts.scheme, parts.netloc)
            return body
        except BaseException:
            _drop_connection(parts.scheme, parts.netloc)
            raise
    raise TileSourceError(f"too many redirects fetching {url}")


def _get_urllib(url: str, headers: dict[str, str], byte_range: tuple[int, int] | None) -> bytes | None:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=HTTP_TIMEOUT) as resp:
            if resp.status == 204:
                return None
            return _body(resp, url, byte_range)[0]
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise _HttpStatusError(exc.code, exc.headers.get("Retry-After") if exc.headers else None) from exc


def _body(resp: Any, url: str, byte_range: tuple[int, int] | None) -> tuple[bytes, bool]:
    """Read a response body; returns ``(bytes, read_to_the_end)``."""
    if byte_range is not None and resp.status == 200:
        # The server ignored Range and is sending the whole file. Read up to the wanted
        # bytes when they are near the start; refuse rather than stream a huge archive.
        end = byte_range[0] + byte_range[1]
        if end > MAX_HTTP_BYTES:
            raise TileSourceError(f"{url} does not support HTTP range requests, which reading PMTiles needs")
        return _read_capped(resp, url, end)[byte_range[0] :], False
    return _read_capped(resp, url, MAX_HTTP_BYTES), True


def http_get(url: str, byte_range: tuple[int, int] | None = None) -> bytes | None:
    """GET a URL (optionally a byte range); None on 404/204. Retries 429 and 5xx with backoff."""
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    if byte_range is not None:
        headers["Range"] = f"bytes={byte_range[0]}-{byte_range[0] + byte_range[1] - 1}"
    delay = 0.5
    for attempt in range(HTTP_RETRIES + 1):
        try:
            return _get_once(url, headers, byte_range)
        except _HttpStatusError as exc:
            if (exc.code == 429 or exc.code >= 500) and attempt < HTTP_RETRIES:
                ra = exc.retry_after
                time.sleep(min(float(ra) if ra and ra.isdigit() else delay, 30.0))
                delay *= 2
                continue
            raise TileSourceError(f"HTTP {exc.code} fetching {url}") from exc
        except (http.client.HTTPException, urllib.error.URLError, OSError) as exc:
            if attempt < HTTP_RETRIES:
                time.sleep(delay)
                delay *= 2
                continue
            raise TileSourceError(f"cannot fetch {url}: {exc}") from exc
    raise TileSourceError(f"cannot fetch {url}")  # pragma: no cover


def _read_capped(resp: Any, url: str, limit: int) -> bytes:
    """Read at most ``limit`` bytes; a longer body is an error unless the caller wanted a prefix."""
    body: bytes = resp.read(limit + 1)
    if len(body) > limit:
        if limit < MAX_HTTP_BYTES:
            return body[:limit]
        raise TileSourceError(f"response from {url} is larger than {MAX_HTTP_BYTES >> 20} MiB")
    return body


def _read_file(path: Path) -> bytes:
    if path.stat().st_size > MAX_HTTP_BYTES:
        raise TileSourceError(f"{path} is larger than {MAX_HTTP_BYTES >> 20} MiB; that is not a single vector tile")
    return path.read_bytes()


def read_bytes(source: str) -> bytes:
    """Read a whole local file or URL."""
    if is_url(source):
        data = http_get(source)
        if data is None:
            raise TileSourceError(f"HTTP 404 fetching {source}")
        return data
    try:
        return _read_file(Path(source).expanduser())
    except OSError as exc:
        raise TileSourceError(f"cannot read {source}: {exc.strerror or exc}") from exc


# ---------------------------------------------------------------- model


@dataclass(slots=True, frozen=True)
class LayerInfo:
    """One entry of a tileset's ``vector_layers`` metadata."""

    id: str
    fields: dict[str, str]
    description: str | None = None
    minzoom: int | None = None
    maxzoom: int | None = None


@dataclass(slots=True)
class TilesetInfo:
    """What a tileset says about itself."""

    format: str
    minzoom: int | None
    maxzoom: int | None
    bounds: BBox | None
    metadata: dict[str, Any]
    layers: list[LayerInfo]


def parse_vector_layers(raw: Any) -> list[LayerInfo]:
    """Read the TileJSON-style ``vector_layers`` list, tolerating missing pieces."""
    out: list[LayerInfo] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict) or "id" not in item:
            continue
        fields = item.get("fields") or {}
        out.append(
            LayerInfo(
                id=str(item["id"]),
                fields={str(k): str(v) for k, v in fields.items()} if isinstance(fields, dict) else {},
                description=item.get("description") or None,
                minzoom=_int_or_none(item.get("minzoom")),
                maxzoom=_int_or_none(item.get("maxzoom")),
            )
        )
    return out


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class TileSource(Protocol):
    """A collection of tiles addressed by XYZ ``z/x/y``."""

    def info(self) -> TilesetInfo:
        """Return the tileset metadata."""
        ...

    def tiles(self, z: int, bbox: BBox | None, limit: int) -> list[TileKey]:
        """List the tiles at zoom ``z`` (inside ``bbox``); raise if more than ``limit``."""
        ...

    def fetch(self, keys: Iterable[TileKey]) -> list[tuple[TileKey, bytes]]:
        """Return the decompressed bytes of the tiles that exist, in order."""
        ...


# ---------------------------------------------------------------- PMTiles


class _ByteSource:
    """Random-access bytes from a local file (mmap) or a URL (range requests, LRU cached)."""

    def __init__(self, source: str) -> None:
        self.source = source
        self._lock = threading.Lock()
        self._cache: OrderedDict[tuple[int, int], bytes] = OrderedDict()
        self._cache_bytes = 0
        if is_url(source):
            self._mmap: mmap.mmap | None = None
            head = http_get(source, (0, HEAD_BYTES))
            if not head:
                raise TileSourceError(f"HTTP 404 fetching {source}")
            self._head = head
        else:
            try:
                fh = Path(source).expanduser().open("rb")
            except OSError as exc:
                raise TileSourceError(f"cannot open {source}: {exc.strerror or exc}") from exc
            with fh:
                self._mmap = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
            self._head = self._mmap[:HEAD_BYTES]

    def get(self, offset: int, length: int, *, cache: bool = True) -> bytes:
        if offset + length <= len(self._head):
            return self._head[offset : offset + length]
        if self._mmap is not None:
            return self._mmap[offset : offset + length]
        key = (offset, length)
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit
        data = http_get(self.source, key)
        if data is None:
            raise TileSourceError(f"HTTP 404 reading bytes {offset}+{length} of {self.source}")
        if cache:
            with self._lock:
                self._cache[key] = data
                self._cache_bytes += len(data)
                while self._cache_bytes > 64 << 20 and self._cache:
                    _, old = self._cache.popitem(last=False)
                    self._cache_bytes -= len(old)
        return data


def _decompress(data: bytes, compression: Any) -> bytes:
    if compression in (Compression.BROTLI, Compression.ZSTD):
        raise TileSourceError(f"PMTiles {compression.name.lower()} compression is not supported (only gzip/none)")
    if compression == Compression.NONE:
        return data
    return maybe_decompress(data)


def _archive_part(data: bytes, compression: Any, what: str) -> bytes:
    """Decompress a PMTiles directory or metadata block; damage is a source error."""
    try:
        return _decompress(data, compression)
    except MvtDecodeError as exc:
        raise TileSourceError(f"corrupt PMTiles {what}: {exc}") from exc


def parse_directory(buf: bytes) -> list[Entry]:
    """Decode an (already decompressed) PMTiles v3 directory.

    ``pmtiles.tile.deserialize_directory`` always gunzips, which breaks archives
    whose internal compression is ``none``; this takes plain bytes instead.
    """
    try:
        n, pos = read_varint(buf, 0)
        ids: list[int] = []
        last = 0
        for _ in range(n):
            d, pos = read_varint(buf, pos)
            last += d
            ids.append(last)
        cols: list[list[int]] = []
        for _ in range(3):  # run_lengths, lengths, offsets
            col: list[int] = []
            for _ in range(n):
                v, pos = read_varint(buf, pos)
                col.append(v)
            cols.append(col)
    except MvtDecodeError as exc:
        raise TileSourceError(f"corrupt PMTiles directory: {exc}") from exc
    entries: list[Entry] = []
    for i in range(n):
        raw_offset, length = cols[2][i], cols[1][i]
        offset = entries[i - 1].offset + entries[i - 1].length if i > 0 and raw_offset == 0 else raw_offset - 1
        entries.append(Entry(ids[i], offset, length, cols[0][i]))
    return entries


class PMTilesSource:
    """A PMTiles v3 archive, local or remote."""

    def __init__(self, source: str) -> None:
        """Open the archive and read its header."""
        self.bytes = _ByteSource(source)
        try:
            self.header = deserialize_header(self.bytes.get(0, 127))
        except Exception as exc:
            raise TileSourceError(f"{source} is not a PMTiles v3 archive: {exc}") from exc
        if self.header.get("tile_type") not in (TileType.MVT, TileType.UNKNOWN):
            raise TileSourceError(f"{source} holds {self.header.get('tile_type')} tiles, not vector tiles")
        self._dirs: OrderedDict[tuple[int, int], list[Any]] = OrderedDict()
        self._dirs_lock = threading.Lock()

    def _directory(self, offset: int, length: int) -> list[Any]:
        key = (offset, length)
        with self._dirs_lock:
            d = self._dirs.get(key)
            if d is not None:
                self._dirs.move_to_end(key)
                return d
        raw = _archive_part(self.bytes.get(offset, length), self.header["internal_compression"], "directory")
        d = parse_directory(raw)
        with self._dirs_lock:
            self._dirs[key] = d
            # A leaf holds up to a few thousand entries; 256 bounds memory on planet-scale archives.
            while len(self._dirs) > DIRECTORY_CACHE:
                self._dirs.popitem(last=False)
        return d

    def info(self) -> TilesetInfo:
        """Return the header + JSON metadata."""
        h = self.header
        raw = self.bytes.get(h["metadata_offset"], h["metadata_length"]) if h["metadata_length"] else b"{}"
        try:
            meta = json.loads(_archive_part(raw, h["internal_compression"], "metadata") or b"{}")
        except ValueError:
            meta = {}
        bounds = (h["min_lon_e7"] / 1e7, h["min_lat_e7"] / 1e7, h["max_lon_e7"] / 1e7, h["max_lat_e7"] / 1e7)
        return TilesetInfo(
            format="pmtiles",
            minzoom=h["min_zoom"],
            maxzoom=h["max_zoom"],
            bounds=bounds,
            metadata=meta if isinstance(meta, dict) else {},
            layers=parse_vector_layers(meta.get("vector_layers") if isinstance(meta, dict) else None),
        )

    def _lookup(self, tile_id: int) -> tuple[int, int] | None:
        h = self.header
        offset, length = h["root_offset"], h["root_length"]
        for _depth in range(4):
            entry = find_tile(self._directory(offset, length), tile_id)
            if entry is None:
                return None
            if entry.run_length == 0:
                offset, length = h["leaf_directory_offset"] + entry.offset, entry.length
                continue
            return h["tile_data_offset"] + entry.offset, entry.length
        return None

    def tiles(self, z: int, bbox: BBox | None, limit: int) -> list[TileKey]:
        """Enumerate addressed tiles: directory walk for a whole zoom, lookups inside a bbox."""
        if bbox is not None:
            x0, y0, x1, y1 = tile_range(z, *bbox)
            # Sparse archives may hold far fewer tiles than the bbox covers: allow 4x candidates.
            _check_count(z, (x1 - x0 + 1) * (y1 - y0 + 1), limit * 4, shown=limit)
            keys: list[TileKey] = []
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    if self._lookup(zxy_to_tileid(z, x, y)) is not None:
                        keys.append((z, x, y))
                        _check_count(z, len(keys), limit)
            return keys
        lo, hi = zxy_to_tileid(z, 0, 0), zxy_to_tileid(z + 1, 0, 0)
        found: list[TileKey] = []
        self._walk(self.header["root_offset"], self.header["root_length"], lo, hi, found, limit)
        _check_count(z, len(found), limit)
        found.sort(key=lambda k: (k[1], k[2]))
        return found

    def _walk(self, offset: int, length: int, lo: int, hi: int, found: list[TileKey], limit: int) -> None:
        entries = self._directory(offset, length)
        for i, e in enumerate(entries):
            nxt = entries[i + 1].tile_id if i + 1 < len(entries) else 1 << 62
            if e.run_length == 0:
                if nxt > lo and e.tile_id < hi:
                    self._walk(self.header["leaf_directory_offset"] + e.offset, e.length, lo, hi, found, limit)
            else:
                for t in range(max(e.tile_id, lo), min(e.tile_id + e.run_length, hi)):
                    found.append(tileid_to_zxy(t))
            if len(found) > limit:
                return

    def fetch(self, keys: Iterable[TileKey]) -> list[tuple[TileKey, bytes]]:
        """Read tiles; remote reads are coalesced into few range requests, run concurrently."""
        h = self.header
        keys = list(keys)
        remote = is_url(self.bytes.source)
        locs = [(k, self._lookup(zxy_to_tileid(*k))) for k in keys]
        found = [(k, loc) for k, loc in locs if loc is not None]
        raw: dict[tuple[int, int], bytes] = {}
        if remote:
            # Tiles are stored in Hilbert order, so nearby tiles are nearby bytes: one request
            # per run instead of one per tile. Runs that share bytes (deduplicated ocean tiles)
            # collapse too.
            spans = _coalesce(sorted({loc for _, loc in found}))

            def read_span(span: tuple[int, int, list[tuple[int, int]]]) -> None:
                start, length, members = span
                blob = self.bytes.get(start, length, cache=False)
                for off, n in members:
                    raw[(off, n)] = blob[off - start : off - start + n]

            if len(spans) <= 1:
                for sp in spans:
                    read_span(sp)
            else:
                with ThreadPoolExecutor(max_workers=min(FETCH_THREADS, len(spans))) as pool:
                    list(pool.map(read_span, spans))
        out: list[tuple[TileKey, bytes]] = []
        for k, loc in found:
            data = raw[loc] if remote else self.bytes.get(loc[0], loc[1], cache=False)
            with tile_context(*k):
                out.append((k, _decompress(data, h["tile_compression"])))
        return out


# ---------------------------------------------------------------- MBTiles


class MBTilesSource:
    """An MBTiles SQLite file (TMS rows are flipped to XYZ)."""

    def __init__(self, source: str) -> None:
        """Open the database read-only."""
        if is_url(source):
            raise TileSourceError("MBTiles must be a local file; download it first or use PMTiles for URLs")
        path = Path(source).expanduser()
        if not path.is_file():
            raise TileSourceError(f"cannot open {source}: no such file")
        self.path = path
        try:
            self.db = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True, check_same_thread=False)
            self.db.execute("SELECT 1 FROM tiles LIMIT 1").fetchall()
        except sqlite3.DatabaseError as exc:
            raise TileSourceError(f"{source} is not an MBTiles database: {exc}") from exc
        self._lock = threading.Lock()

    def info(self) -> TilesetInfo:
        """Return the ``metadata`` table, with ``json`` expanded."""
        with self._lock:
            rows = self.db.execute("SELECT name, value FROM metadata").fetchall()
        meta: dict[str, Any] = {str(k): v for k, v in rows}
        extra: dict[str, Any] = {}
        if isinstance(meta.get("json"), str):
            try:
                parsed = json.loads(meta["json"])
                extra = parsed if isinstance(parsed, dict) else {}
            except ValueError:
                extra = {}
        bounds: BBox | None = None
        if isinstance(meta.get("bounds"), str):
            try:
                w, s, e, n = (float(p) for p in meta["bounds"].split(","))
                bounds = (w, s, e, n)
            except ValueError:
                bounds = None
        minzoom, maxzoom = _int_or_none(meta.get("minzoom")), _int_or_none(meta.get("maxzoom"))
        if minzoom is None or maxzoom is None:
            with self._lock:
                lo, hi = self.db.execute("SELECT min(zoom_level), max(zoom_level) FROM tiles").fetchone()
            minzoom = lo if minzoom is None else minzoom
            maxzoom = hi if maxzoom is None else maxzoom
        return TilesetInfo(
            format="mbtiles",
            minzoom=minzoom,
            maxzoom=maxzoom,
            bounds=bounds,
            metadata={**{k: v for k, v in meta.items() if k != "json"}, **extra},
            layers=parse_vector_layers(extra.get("vector_layers")),
        )

    def tiles(self, z: int, bbox: BBox | None, limit: int) -> list[TileKey]:
        """List tiles at zoom ``z``, filtered in SQL."""
        n = 1 << z
        sql = "SELECT tile_column, tile_row FROM tiles WHERE zoom_level = ?"
        params: list[int] = [z]
        if bbox is not None:
            x0, y0, x1, y1 = tile_range(z, *bbox)
            sql += " AND tile_column BETWEEN ? AND ? AND tile_row BETWEEN ? AND ?"
            params += [x0, x1, n - 1 - y1, n - 1 - y0]
        sql += f" ORDER BY tile_column, tile_row DESC LIMIT {int(limit) + 1}"
        with self._lock:
            rows = self.db.execute(sql, params).fetchall()
        _check_count(z, len(rows), limit)
        return [(z, int(c), n - 1 - int(r)) for c, r in rows]

    def fetch(self, keys: Iterable[TileKey]) -> list[tuple[TileKey, bytes]]:
        """Read tiles from SQLite."""
        out: list[tuple[TileKey, bytes]] = []
        with self._lock:
            for z, x, y in keys:
                row = self.db.execute(
                    "SELECT tile_data FROM tiles WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?",
                    (z, x, (1 << z) - 1 - y),
                ).fetchone()
                if row is not None and row[0]:
                    with tile_context(z, x, y):
                        out.append(((z, x, y), maybe_decompress(bytes(row[0]))))
        return out


# ---------------------------------------------------------------- XYZ template


class XYZSource:
    """A tile server URL (or local path) template with ``{z}``, ``{x}`` and ``{y}``."""

    def __init__(self, template: str) -> None:
        """Remember the template; nothing is fetched until tiles are read."""
        for part in ("{z}", "{x}", "{y}"):
            if part not in template:
                raise TileSourceError(f"tile URL template must contain {{z}}, {{x}} and {{y}}: {template}")
        self.template = template

    def info(self) -> TilesetInfo:
        """Templates carry no metadata."""
        return TilesetInfo(format="xyz", minzoom=None, maxzoom=None, bounds=None, metadata={}, layers=[])

    def tiles(self, z: int, bbox: BBox | None, limit: int) -> list[TileKey]:
        """Every tile in the bbox; a template cannot say which exist."""
        if bbox is None:
            raise TileSourceError("reading an XYZ tile template needs a bbox (west, south, east, north)")
        x0, y0, x1, y1 = tile_range(z, *bbox)
        _check_count(z, (x1 - x0 + 1) * (y1 - y0 + 1), limit)
        return [(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]

    def fetch(self, keys: Iterable[TileKey]) -> list[tuple[TileKey, bytes]]:
        """Fetch tiles concurrently; missing tiles (404/204) are skipped."""

        def one(key: TileKey) -> tuple[TileKey, bytes] | None:
            z, x, y = key
            url = self.template.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
            if is_url(url):
                data = http_get(url)
            else:
                p = Path(url).expanduser()
                data = _read_file(p) if p.is_file() else None
            if not data:
                return None
            with tile_context(*key):
                return key, maybe_decompress(data)

        return _gather(one, list(keys), threads=FETCH_THREADS if is_url(self.template) else 1)


# Two tiles are fetched in one range request when the bytes between them are fewer than
# COALESCE_GAP; one request never spans more than COALESCE_MAX.
COALESCE_GAP = 64 << 10
COALESCE_MAX = 8 << 20


def _coalesce(locs: list[tuple[int, int]]) -> list[tuple[int, int, list[tuple[int, int]]]]:
    """Group sorted ``(offset, length)`` reads into ``(start, length, members)`` spans."""
    spans: list[tuple[int, int, list[tuple[int, int]]]] = []
    for off, n in locs:
        if spans:
            start, length, members = spans[-1]
            end = start + length
            if off - end <= COALESCE_GAP and max(end, off + n) - start <= COALESCE_MAX:
                members.append((off, n))
                spans[-1] = (start, max(end, off + n) - start, members)
                continue
        spans.append((off, n, [(off, n)]))
    return spans


def _check_count(z: int, count: int, limit: int, *, shown: int | None = None) -> None:
    if count > limit:
        raise TileSourceError(
            f"more than {shown or limit} tiles at zoom {z} (max_tiles); narrow the area with "
            "WHERE ST_Intersects(geometry, <box>), the west/south/east/north arguments or the bbox "
            "ATTACH option, pick a lower zoom, or raise max_tiles"
        )


def _gather(
    fn: Callable[[TileKey], tuple[TileKey, bytes] | None], keys: list[TileKey], *, threads: int
) -> list[tuple[TileKey, bytes]]:
    if threads <= 1 or len(keys) <= 1:
        results = [fn(k) for k in keys]
    else:
        with ThreadPoolExecutor(max_workers=min(threads, len(keys))) as pool:
            results = list(pool.map(fn, keys))
    return [r for r in results if r is not None]


# ---------------------------------------------------------------- opening

_OPEN: OrderedDict[str, TileSource] = OrderedDict()
_OPEN_LOCK = threading.Lock()


def source_format(source: str) -> str:
    """Classify a source string: pmtiles, mbtiles or xyz."""
    if "{z}" in source:
        return "xyz"
    path = source.split("?", 1)[0].lower()
    if path.endswith(".mbtiles"):
        return "mbtiles"
    if path.endswith(".pmtiles"):
        return "pmtiles"
    if not is_url(source):
        try:
            with Path(source).expanduser().open("rb") as fh:
                magic = fh.read(16)
        except OSError as exc:
            raise TileSourceError(f"cannot open {source}: {exc.strerror or exc}") from exc
        if magic.startswith(b"PMTiles"):
            return "pmtiles"
        if magic.startswith(b"SQLite format 3"):
            return "mbtiles"
    raise TileSourceError(
        f"cannot tell the tileset format of {source}; use a .pmtiles or .mbtiles file, "
        "or a URL template containing {z}/{x}/{y}"
    )


def open_source(source: str) -> TileSource:
    """Open (and cache per process) the tileset behind ``source``."""
    key = source
    if not is_url(source) and "{z}" not in source:
        try:
            key = f"{source}\0{Path(source).expanduser().stat().st_mtime_ns}"
        except OSError:
            key = source
    with _OPEN_LOCK:
        hit = _OPEN.get(key)
        if hit is not None:
            _OPEN.move_to_end(key)
            return hit
    fmt = source_format(source)
    opened: TileSource
    if fmt == "pmtiles":
        opened = PMTilesSource(source)
    elif fmt == "mbtiles":
        opened = MBTilesSource(source)
    else:
        opened = XYZSource(source)
    with _OPEN_LOCK:
        _OPEN[key] = opened
        while len(_OPEN) > 16:
            _OPEN.popitem(last=False)
    return opened
