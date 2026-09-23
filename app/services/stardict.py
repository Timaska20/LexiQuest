from __future__ import annotations

import gzip
import struct
from functools import lru_cache
from pathlib import Path


class StarDictError(Exception):
    pass


class StarDictDictionary:
    """Read-only StarDict reader for text dictionaries.

    Supports .ifo + .idx + .dict/.dict.dz and optional .syn aliases. Text-only
    entries are exposed as plain strings; binary resource fields are skipped.
    """

    def __init__(self, base_path: Path):
        self.base_path = base_path.with_suffix("") if base_path.suffix == ".ifo" else base_path
        self.ifo_path = Path(str(self.base_path) + ".ifo")
        self.idx_path = Path(str(self.base_path) + ".idx")
        self.dict_path = Path(str(self.base_path) + ".dict")
        self.dict_dz_path = Path(str(self.base_path) + ".dict.dz")
        self.syn_path = Path(str(self.base_path) + ".syn")
        self.meta = self._read_ifo()
        self.index, self.index_order = self._read_idx()
        self._read_syn()

    def _read_ifo(self) -> dict[str, str]:
        if not self.ifo_path.exists():
            raise StarDictError(f"Missing {self.ifo_path.name}")
        meta = {}
        for line in self.ifo_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                meta[key.strip()] = value.strip()
        return meta

    def _read_idx(self) -> tuple[dict[str, tuple[int, int]], list[tuple[str, int, int]]]:
        if not self.idx_path.exists():
            raise StarDictError(f"Missing {self.idx_path.name}")
        data = self.idx_path.read_bytes()
        offset_bits = int(self.meta.get("idxoffsetbits", "32"))
        offset_fmt = ">Q" if offset_bits == 64 else ">I"
        offset_len = 8 if offset_bits == 64 else 4
        out: dict[str, tuple[int, int]] = {}
        order: list[tuple[str, int, int]] = []
        p = 0
        while p < len(data):
            end = data.find(b"\x00", p)
            if end < 0:
                break
            word = data[p:end].decode("utf-8", errors="ignore")
            p = end + 1
            if p + offset_len + 4 > len(data):
                break
            offset = struct.unpack(offset_fmt, data[p : p + offset_len])[0]
            p += offset_len
            size = struct.unpack(">I", data[p : p + 4])[0]
            p += 4
            if word:
                key = word.casefold()
                out[key] = (offset, size)
                order.append((word, offset, size))
        return out, order

    def _read_syn(self) -> None:
        if not self.syn_path.exists():
            return
        data = self.syn_path.read_bytes()
        p = 0
        while p < len(data):
            end = data.find(b"\x00", p)
            if end < 0 or end + 5 > len(data):
                break
            alias = data[p:end].decode("utf-8", errors="ignore")
            p = end + 1
            idx = struct.unpack(">I", data[p : p + 4])[0]
            p += 4
            if alias and 0 <= idx < len(self.index_order):
                _, offset, size = self.index_order[idx]
                self.index.setdefault(alias.casefold(), (offset, size))

    def lookup(self, word: str) -> str | None:
        item = None
        for candidate in word_variants(word):
            item = self.index.get(candidate.casefold())
            if item:
                break
        if not item:
            return None
        offset, size = item
        if self.dict_path.exists():
            with self.dict_path.open("rb") as fh:
                fh.seek(offset)
                raw = fh.read(size)
        elif self.dict_dz_path.exists():
            # dictzip is gzip-compatible for sequential reads. For an MVP this
            # is fine; a future index cache can optimize very large dictionaries.
            with gzip.open(self.dict_dz_path, "rb") as fh:
                fh.seek(offset)
                raw = fh.read(size)
        else:
            raise StarDictError("Missing .dict or .dict.dz")

        return self._decode_entry(raw)

    def _decode_entry(self, raw: bytes) -> str:
        same = self.meta.get("sametypesequence", "")
        if same:
            return raw.decode("utf-8", errors="replace").replace("\x00", " ").strip()

        # Without sametypesequence, each field begins with a type byte. Lowercase
        # types are NUL-terminated text; uppercase types are 32-bit-sized binary.
        pieces: list[str] = []
        p = 0
        while p < len(raw):
            type_chr = chr(raw[p])
            p += 1
            if type_chr.islower():
                end = raw.find(b"\x00", p)
                if end < 0:
                    end = len(raw)
                text = raw[p:end].decode("utf-8", errors="replace").strip()
                if text:
                    pieces.append(text)
                p = min(end + 1, len(raw))
            else:
                if p + 4 > len(raw):
                    break
                length = struct.unpack(">I", raw[p : p + 4])[0]
                p += 4 + length
        return "\n".join(pieces).strip()


def word_variants(word: str) -> list[str]:
    """Small English-friendly fallback without hard-coding the app to English.

    Exact lookup is always first. The additional forms only help common clicked
    inflections such as dreams -> dream or studies -> study.
    """
    w = word.strip().casefold().replace("’", "'")
    if not w:
        return []
    candidates = [w]

    if w.endswith("'s") and len(w) > 3:
        candidates.append(w[:-2])
    if w.endswith("ies") and len(w) > 4:
        candidates.append(w[:-3] + "y")
    if w.endswith("es") and len(w) > 4:
        candidates.extend([w[:-2], w[:-1]])
    elif w.endswith("s") and len(w) > 3 and not w.endswith("ss"):
        candidates.append(w[:-1])
    if w.endswith("ing") and len(w) > 5:
        stem = w[:-3]
        candidates.extend([stem, stem + "e"])
        if len(stem) >= 2 and stem[-1] == stem[-2]:
            candidates.append(stem[:-1])
    if w.endswith("ed") and len(w) > 4:
        stem = w[:-2]
        candidates.extend([stem, stem + "e"])
        if stem.endswith("i"):
            candidates.append(stem[:-1] + "y")

    seen = set()
    return [x for x in candidates if x and not (x in seen or seen.add(x))]


@lru_cache(maxsize=32)
def get_stardict(base_path: str) -> StarDictDictionary:
    """Cache parsed .idx data so large dictionaries do not reload on every tap."""
    return StarDictDictionary(Path(base_path))
