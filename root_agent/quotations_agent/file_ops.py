# vendors_agent/file_ops.py
"""
Generalised local file-system helpers.
All operations are scoped to a user-provided storage directory.
No external dependencies beyond Python standard library.
"""

import sys
from pathlib import Path

# ── Anchor
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import csv
import datetime
import io
import json
import shutil
from typing import Any, Callable, Union

from logger import get_logger

logger = get_logger(__name__)

# Convenience type alias
ContentType = Union[str, bytes, dict, list]


# ─────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────

def _resolve_storage_dir(storage_dir: str | Path) -> Path:
    base = Path(storage_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    return base


def _validate_within_directory(target: Path, base: Path) -> None:
    if not str(target).startswith(str(base)):
        raise ValueError(f"Operation blocked: '{target}' is outside '{base}'")


# ─────────────────────────────────────────────────────────────
# INTERNAL — IO handlers (type-aware read / write)
# ─────────────────────────────────────────────────────────────

_TEXT_EXTENSIONS  = {
    ".txt", ".md", ".html", ".htm", ".css", ".js", ".ts",
    ".xml", ".yaml", ".yml", ".toml", ".ini", ".env",
    ".log", ".rst", ".tex",
}
_JSON_EXTENSIONS  = {".json", ".jsonc"}
_CSV_EXTENSIONS   = {".csv", ".tsv"}
_BINARY_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".mp3", ".mp4", ".zip", ".tar", ".gz", ".bin", ".exe",
    ".docx", ".xlsx", ".pptx",
}


def _read_content(path: Path, encoding: str = "utf-8",
                  delimiter: str = ",", **_) -> ContentType:
    ext = path.suffix.lower()
    if ext in _JSON_EXTENSIONS:
        with path.open("r", encoding=encoding) as f:
            return json.load(f)
    if ext in _CSV_EXTENSIONS:
        sep = "\t" if ext == ".tsv" else delimiter
        with path.open("r", encoding=encoding, newline="") as f:
            return list(csv.DictReader(f, delimiter=sep))
    if ext in _BINARY_EXTENSIONS:
        return path.read_bytes()
    # Default: treat as plain text (covers .txt, .md, .log, unknown, …)
    return path.read_text(encoding=encoding)


def _write_content(path: Path, content: ContentType,
                   encoding: str = "utf-8", indent: int = 2,
                   delimiter: str = ",", **_) -> None:
    ext = path.suffix.lower()

    if ext in _JSON_EXTENSIONS:
        if not isinstance(content, (dict, list)):
            raise TypeError(
                f"JSON file expects dict or list content, got {type(content).__name__}."
            )
        with path.open("w", encoding=encoding) as f:
            json.dump(content, f, indent=indent, ensure_ascii=False)
        return

    if ext in _CSV_EXTENSIONS:
        if not isinstance(content, list):
            raise TypeError(
                f"CSV file expects list[dict] content, got {type(content).__name__}."
            )
        sep = "\t" if ext == ".tsv" else delimiter
        with path.open("w", encoding=encoding, newline="") as f:
            if content:
                writer = csv.DictWriter(f, fieldnames=content[0].keys(), delimiter=sep)
                writer.writeheader()
                writer.writerows(content)
        return

    if ext in _BINARY_EXTENSIONS:
        if not isinstance(content, bytes):
            raise TypeError(
                f"Binary file expects bytes content, got {type(content).__name__}."
            )
        path.write_bytes(content)
        return

    # Default: plain text
    if not isinstance(content, str):
        raise TypeError(
            f"Text file expects str content, got {type(content).__name__}."
        )
    path.write_text(content, encoding=encoding)


# ─────────────────────────────────────────────────────────────
# COPY / SAVE
# ─────────────────────────────────────────────────────────────

def save_file(src_path: str, storage_dir: str) -> str:
    base      = _resolve_storage_dir(storage_dir)
    src       = Path(src_path)
    timestamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    dst       = base / f"{timestamp}_{src.name}"
    shutil.copy2(src, dst)
    logger.info(f"[file_ops] Saved: {src} → {dst}")
    return str(dst)


# ─────────────────────────────────────────────────────────────
# DELETE
# ─────────────────────────────────────────────────────────────

def delete_file(stored_path: str, storage_dir: str) -> bool:
    base   = _resolve_storage_dir(storage_dir)
    target = Path(stored_path).resolve()
    _validate_within_directory(target, base)
    if not target.exists():
        logger.warning(f"[file_ops] Delete skipped – not found: {target}")
        return False
    target.unlink()
    logger.info(f"[file_ops] Deleted: {target}")
    return True


# ─────────────────────────────────────────────────────────────
# REPLACE / OVERWRITE  (swap the whole file with a new source)
# ─────────────────────────────────────────────────────────────

def replace_file(stored_path: str, new_src_path: str, storage_dir: str) -> str:
    base   = _resolve_storage_dir(storage_dir)
    target = Path(stored_path).resolve()
    _validate_within_directory(target, base)
    shutil.copy2(new_src_path, target)
    logger.info(f"[file_ops] Replaced: {target} ← {new_src_path}")
    return str(target)


# ─────────────────────────────────────────────────────────────
# WRITE TEXT  (legacy helper — .txt / .md only)
# ─────────────────────────────────────────────────────────────

def write_text(stored_path: str, content: str, storage_dir: str) -> str:
    base   = _resolve_storage_dir(storage_dir)
    target = Path(stored_path).resolve()
    _validate_within_directory(target, base)
    if target.suffix.lower() not in {".txt", ".md"}:
        raise ValueError(f"write_text supports only .txt / .md, got: {target.suffix}")
    target.write_text(content, encoding="utf-8")
    logger.info(f"[file_ops] Text written: {target}")
    return str(target)


# ─────────────────────────────────────────────────────────────
# LIST FILES
# ─────────────────────────────────────────────────────────────

def list_stored_files(storage_dir: str) -> list[dict]:
    base    = _resolve_storage_dir(storage_dir)
    entries = []
    for p in sorted(base.iterdir()):
        if p.is_file():
            stat = p.stat()
            entries.append({
                "name":     p.name,
                "path":     str(p),
                "size_kb":  round(stat.st_size / 1024, 2),
                "modified": datetime.datetime.utcfromtimestamp(
                    stat.st_mtime
                ).strftime("%Y-%m-%d %H:%M:%S UTC"),
            })
    return entries


# ─────────────────────────────────────────────────────────────
# EXISTS
# ─────────────────────────────────────────────────────────────

def file_exists(stored_path: str, storage_dir: str) -> bool:
    base   = _resolve_storage_dir(storage_dir)
    target = Path(stored_path).resolve()
    try:
        _validate_within_directory(target, base)
    except ValueError:
        return False
    return target.exists()


# ─────────────────────────────────────────────────────────────
# READ FILE CONTENT  ← NEW
# ─────────────────────────────────────────────────────────────

def read_file_content(
    stored_path: str,
    storage_dir: str,
    *,
    encoding: str = "utf-8",
    delimiter: str = ",",
) -> ContentType:
    """
    Read the content of a stored file and return it in its natural Python type:

      .json / .jsonc  → dict or list
      .csv  / .tsv    → list[dict]
      binary formats  → bytes
      everything else → str

    Parameters
    ----------
    stored_path : absolute path to the file (must be inside storage_dir)
    storage_dir : root directory that scopes all file operations
    encoding    : text encoding (default utf-8)
    delimiter   : CSV field separator (default ',')
    """
    base   = _resolve_storage_dir(storage_dir)
    target = Path(stored_path).resolve()
    _validate_within_directory(target, base)

    if not target.exists():
        raise FileNotFoundError(f"[file_ops] File not found: {target}")

    content = _read_content(target, encoding=encoding, delimiter=delimiter)
    logger.info(f"[file_ops] Read content: {target}  (type={type(content).__name__})")
    return content


# ─────────────────────────────────────────────────────────────
# UPDATE FILE CONTENT  ← NEW
# ─────────────────────────────────────────────────────────────

def update_file_content(
    stored_path: str,
    new_content: ContentType,
    storage_dir: str,
    *,
    encoding: str = "utf-8",
    indent: int = 2,
    delimiter: str = ",",
    backup: bool = False,
) -> str:
    """
    Overwrite a stored file's content in-place.

    The write strategy is chosen automatically from the file's extension:

      .json / .jsonc  → json.dump  (pass indent= to control formatting)
      .csv  / .tsv    → csv.DictWriter  (pass delimiter= if needed)
      binary formats  → path.write_bytes
      everything else → path.write_text

    Parameters
    ----------
    stored_path : absolute path to the target file
    new_content : replacement content (must match the file's expected type)
    storage_dir : root directory that scopes all file operations
    encoding    : text encoding (default utf-8)
    indent      : JSON indent width (default 2)
    delimiter   : CSV field separator (default ',')
    backup      : if True, save a .bak copy before overwriting (default False)

    Returns
    -------
    str — the resolved absolute path that was written
    """
    base   = _resolve_storage_dir(storage_dir)
    target = Path(stored_path).resolve()
    _validate_within_directory(target, base)

    if not target.exists():
        raise FileNotFoundError(f"[file_ops] File not found: {target}")

    if backup:
        bak = target.with_suffix(target.suffix + ".bak")
        shutil.copy2(target, bak)
        logger.info(f"[file_ops] Backup created: {bak}")

    _write_content(
        target, new_content,
        encoding=encoding, indent=indent, delimiter=delimiter,
    )
    logger.info(
        f"[file_ops] Content updated: {target}  "
        f"(type={type(new_content).__name__})"
    )
    return str(target)


# ─────────────────────────────────────────────────────────────
# PATCH FILE CONTENT  ← NEW
# ─────────────────────────────────────────────────────────────

def patch_file_content(
    stored_path: str,
    updater_fn: Callable[[ContentType], ContentType],
    storage_dir: str,
    *,
    encoding: str = "utf-8",
    indent: int = 2,
    delimiter: str = ",",
    backup: bool = False,
) -> str:
    """
    Read a file, apply a transform function, and write the result back.

    This is the surgical-edit helper: read → transform → save, all in one call.

    Parameters
    ----------
    stored_path : absolute path to the target file
    updater_fn  : callable that receives the current content and returns
                  the modified version.  Examples:

                    # Append a line to a text file
                    patch_file_content(p, lambda t: t + "\\nnew line", sd)

                    # Bump a version in JSON
                    patch_file_content(p, lambda d: {**d, "version": "2.0"}, sd)

                    # Add a CSV row
                    patch_file_content(p, lambda rows: rows + [{"id": "4", "name": "Dave"}], sd)

    storage_dir : root directory that scopes all file operations
    encoding    : text encoding (default utf-8)
    indent      : JSON indent width (default 2)
    delimiter   : CSV field separator (default ',')
    backup      : if True, save a .bak copy before overwriting (default False)

    Returns
    -------
    str — the resolved absolute path that was written
    """
    current_content = read_file_content(
        stored_path, storage_dir,
        encoding=encoding, delimiter=delimiter,
    )
    updated_content = updater_fn(current_content)
    return update_file_content(
        stored_path, updated_content, storage_dir,
        encoding=encoding, indent=indent,
        delimiter=delimiter, backup=backup,
    )
