from __future__ import annotations

import gzip
import shutil
import uuid
import zipfile
from pathlib import Path

from .stardict import StarDictDictionary, StarDictError


class DictionaryUploadError(ValueError):
    pass


ALLOWED_SUFFIXES = {".ifo", ".idx", ".dict", ".dz", ".syn"}


def install_uploaded_dictionary(
    uploaded_paths: list[Path],
    dictionary_root: Path,
    max_extracted_bytes: int = 1024 * 1024 * 1024,
) -> list[tuple[Path, str]]:
    """Install one or more StarDict dictionaries from ZIP/components.

    Returns [(base_path, book_name), ...]. All extraction happens in a private
    temporary directory and is copied to a generated server-side directory.
    """
    staging = dictionary_root / ".upload-tmp" / uuid.uuid4().hex
    staging.mkdir(parents=True, exist_ok=True)
    try:
        for src in uploaded_paths:
            if src.suffix.lower() == ".zip":
                _safe_extract_zip(src, staging, max_extracted_bytes)
            else:
                if not _is_allowed_component(src.name):
                    raise DictionaryUploadError(f"Unsupported dictionary file: {src.name}")
                shutil.copy2(src, staging / Path(src.name).name)

        ifo_files = list(staging.rglob("*.ifo"))
        if not ifo_files:
            raise DictionaryUploadError("StarDict .ifo file was not found")

        installed: list[tuple[Path, str]] = []
        for ifo in ifo_files:
            base = Path(str(ifo)[:-4])
            _validate_components(base)
            sd = StarDictDictionary(base)
            book_name = sd.meta.get("bookname", ifo.stem).strip() or ifo.stem

            dest_dir = dictionary_root / uuid.uuid4().hex[:12]
            dest_dir.mkdir(parents=True, exist_ok=False)
            dest_base = dest_dir / ifo.stem
            for ext in (".ifo", ".idx", ".dict", ".dict.dz", ".syn"):
                src_component = Path(str(base) + ext)
                if src_component.exists():
                    shutil.copy2(src_component, Path(str(dest_base) + ext))

            # .dict.dz is convenient for transport but very slow if Python has
            # to seek through gzip on every word lookup. Expand it once on
            # upload and keep the compressed original as a backup.
            dest_dict = Path(str(dest_base) + ".dict")
            dest_dz = Path(str(dest_base) + ".dict.dz")
            if not dest_dict.exists() and dest_dz.exists():
                written = 0
                with gzip.open(dest_dz, "rb") as inp, dest_dict.open("wb") as out:
                    while chunk := inp.read(1024 * 1024):
                        written += len(chunk)
                        if written > max_extracted_bytes:
                            dest_dict.unlink(missing_ok=True)
                            raise DictionaryUploadError("Expanded .dict.dz is too large")
                        out.write(chunk)

            # Validate after the final copy before registering it in SQLite.
            StarDictDictionary(dest_base)
            installed.append((dest_base, book_name))

        return installed
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _is_allowed_component(name: str) -> bool:
    lower = name.lower()
    return lower.endswith((".ifo", ".idx", ".dict", ".dict.dz", ".syn"))


def _validate_components(base: Path) -> None:
    missing = []
    if not Path(str(base) + ".ifo").is_file():
        missing.append(".ifo")
    if not Path(str(base) + ".idx").is_file():
        missing.append(".idx")
    if not (Path(str(base) + ".dict").is_file() or Path(str(base) + ".dict.dz").is_file()):
        missing.append(".dict/.dict.dz")
    if missing:
        raise DictionaryUploadError(f"Incomplete StarDict package ({', '.join(missing)} missing) for {base.name}")


def _safe_extract_zip(src: Path, dest: Path, max_extracted_bytes: int) -> None:
    total = 0
    with zipfile.ZipFile(src) as zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        if len(members) > 200:
            raise DictionaryUploadError("Dictionary ZIP contains too many files")

        for member in members:
            total += member.file_size
            if total > max_extracted_bytes:
                raise DictionaryUploadError("Dictionary ZIP is too large after extraction")
            if not _is_allowed_component(member.filename):
                continue

            # Flatten the archive: StarDict components are matched by basename.
            name = Path(member.filename).name
            if not name or name in {".", ".."}:
                continue
            target = dest / name
            with zf.open(member) as inp, target.open("wb") as out:
                shutil.copyfileobj(inp, out, length=1024 * 1024)
