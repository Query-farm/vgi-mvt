"""A tiny MVT encoder for tests: builds tiles with exactly the features a test needs."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def zigzag(n: int) -> int:
    return (n << 1) ^ (n >> 63)


def key(num: int, wire: int) -> bytes:
    return varint((num << 3) | wire)


def ld(num: int, payload: bytes) -> bytes:
    return key(num, 2) + varint(len(payload)) + payload


def packed(num: int, values: list[int]) -> bytes:
    return ld(num, b"".join(varint(v) for v in values))


def command(cid: int, count: int) -> int:
    return (cid & 7) | (count << 3)


def geometry(parts: list[list[tuple[int, int]]], *, close: bool) -> list[int]:
    """Encode parts (point groups, lines or rings) as MVT commands."""
    out: list[int] = []
    cx = cy = 0
    for part in parts:
        x, y = part[0]
        out += [command(1, 1), zigzag(x - cx), zigzag(y - cy)]
        cx, cy = x, y
        rest = part[1:-1] if close and part[-1] == part[0] else part[1:]
        if rest:
            out.append(command(2, len(rest)))
            for x, y in rest:
                out += [zigzag(x - cx), zigzag(y - cy)]
                cx, cy = x, y
        if close:
            out.append(command(7, 1))
    return out


def points(pts: list[tuple[int, int]]) -> list[int]:
    out = [command(1, len(pts))]
    cx = cy = 0
    for x, y in pts:
        out += [zigzag(x - cx), zigzag(y - cy)]
        cx, cy = x, y
    return out


def encode_value(v: Any) -> bytes:
    if isinstance(v, bool):
        return key(7, 0) + varint(int(v))
    if isinstance(v, str):
        return ld(1, v.encode())
    if isinstance(v, float):
        return key(3, 1) + struct.pack("<d", v)
    if isinstance(v, int):
        return key(6, 0) + varint(zigzag(v)) if v < 0 else key(5, 0) + varint(v)
    raise TypeError(v)


@dataclass
class F:
    gtype: int
    commands: list[int]
    props: dict[str, Any] = field(default_factory=dict)
    id: int | None = None


def layer(name: str, features: list[F], *, extent: int = 4096, version: int = 2) -> bytes:
    keys: list[str] = []
    values: list[Any] = []
    body = key(15, 0) + varint(version) + ld(1, name.encode())
    for f in features:
        tags: list[int] = []
        for k, v in f.props.items():
            if k not in keys:
                keys.append(k)
            vk = (type(v).__name__, v)
            if vk not in [(type(x).__name__, x) for x in values]:
                values.append(v)
            tags += [keys.index(k), [(type(x).__name__, x) for x in values].index(vk)]
        fb = b""
        if f.id is not None:
            fb += key(1, 0) + varint(f.id)
        if tags:
            fb += packed(2, tags)
        fb += key(3, 0) + varint(f.gtype) + packed(4, f.commands)
        body += ld(2, fb)
    for k in keys:
        body += ld(3, k.encode())
    for v in values:
        body += ld(4, encode_value(v))
    body += key(5, 0) + varint(extent)
    return body


def tile(*layers: bytes) -> bytes:
    return b"".join(ld(3, lay) for lay in layers)
