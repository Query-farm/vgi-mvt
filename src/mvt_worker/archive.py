"""Write a PMTiles v3 archive: clustered, gzip tiles, identical tiles stored once.

Tiles must arrive in ascending tile id (zoom by zoom, Hilbert order within a
zoom), which makes the archive *clustered*: readers fetch a bounding box's tiles
in a few contiguous byte ranges. Tile bytes are spooled to a temporary file next
to the destination; :meth:`ArchiveWriter.finish` writes header, directories and
metadata, appends the spooled tiles and renames the result over the destination,
so a failed COPY never leaves a half-written archive behind.

The directory and header encoders come from the ``pmtiles`` package. Its own
``Writer`` isn't used: it deduplicates tiles by Python ``hash()`` (a collision
would silently give one tile another's contents) and cannot write an empty
archive.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pmtiles.tile import Compression, Entry, TileType, serialize_header
from pmtiles.writer import optimize_directories

_ROOT_MAX = 16384 - 127  # header + root must fit the first 16 KiB a reader fetches


class ArchiveWriter:
    """Accumulate tiles in id order, then write the archive in one go."""

    def __init__(self, destination: str) -> None:
        """Open a spool file in the destination's directory."""
        self.path = Path(destination)
        if not self.path.parent.is_dir():
            raise ValueError(f"cannot write {destination}: directory {self.path.parent} does not exist")
        self._spool = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed in finish/abort
            dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tiles", delete=False
        )
        self.entries: list[Entry] = []
        self._seen: dict[bytes, tuple[int, int]] = {}
        self.offset = 0
        self.addressed = 0
        self._last_id = -1

    def add(self, tile_id: int, data: bytes) -> None:
        """Append one (already compressed) tile; ids must strictly increase."""
        if tile_id <= self._last_id:
            raise AssertionError(f"tiles out of order: {tile_id} after {self._last_id}")
        self._last_id = tile_id
        self.addressed += 1
        digest = hashlib.blake2b(data, digest_size=16).digest()
        found = self._seen.get(digest)
        if found is None:
            found = (self.offset, len(data))
            self._seen[digest] = found
            self._spool.write(data)
            self.offset += len(data)
        offset, length = found
        last = self.entries[-1] if self.entries else None
        if last is not None and last.offset == offset and last.tile_id + last.run_length == tile_id:
            last.run_length += 1
        else:
            self.entries.append(Entry(tile_id, offset, length, 1))

    @property
    def distinct_tiles(self) -> int:
        """Number of distinct tile contents stored."""
        return len(self._seen)

    def finish(self, header: dict[str, Any], metadata: dict[str, Any]) -> None:
        """Write the archive to the destination (atomically) and remove the spool."""
        root, leaves, _ = optimize_directories(self.entries, _ROOT_MAX)  # type: ignore[no-untyped-call]
        meta = gzip.compress(json.dumps(metadata, separators=(",", ":")).encode(), mtime=0)
        h = dict(header)
        h.update(
            root_offset=127,
            root_length=len(root),
            metadata_offset=127 + len(root),
            metadata_length=len(meta),
            leaf_directory_offset=127 + len(root) + len(meta),
            leaf_directory_length=len(leaves),
            tile_data_offset=127 + len(root) + len(meta) + len(leaves),
            tile_data_length=self.offset,
            addressed_tiles_count=self.addressed,
            tile_entries_count=len(self.entries),
            tile_contents_count=len(self._seen),
            clustered=True,
            internal_compression=Compression.GZIP,
            tile_compression=Compression.GZIP,
            tile_type=TileType.MVT,
        )
        head = serialize_header(h)  # type: ignore[arg-type]
        self._spool.flush()
        out = tempfile.NamedTemporaryFile(  # noqa: SIM115 - renamed or removed below
            dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".part", delete=False
        )
        try:
            with out:
                out.write(head)
                out.write(root)
                out.write(meta)
                out.write(leaves)
                self._spool.seek(0)
                while chunk := self._spool.read(1 << 20):
                    out.write(chunk)
            os.replace(out.name, self.path)
        except BaseException:
            Path(out.name).unlink(missing_ok=True)
            raise
        finally:
            self.abort()

    def abort(self) -> None:
        """Remove the spool file."""
        self._spool.close()
        Path(self._spool.name).unlink(missing_ok=True)
