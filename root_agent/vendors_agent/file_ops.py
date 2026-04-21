# file_ops.py
"""
Generalized local file-system helpers.

All operations are scoped to a user-provided storage directory.
No external dependencies beyond Python standard library.
"""

import shutil
import datetime
from pathlib import Path

from logger import get_logger

logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────
# INTERNAL HELPER
# ─────────────────────────────────────────────────────────────
def _resolve_storage_dir(storage_dir: str | Path) -> Path:
    """
    Ensure the storage directory exists and return resolved Path.
    """
    base = Path(storage_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    return base


def _validate_within_directory(target: Path, base: Path):
    """
    Security check: ensure target path is inside base directory.
    """
    if not str(target).startswith(str(base)):
        raise ValueError(
            f"Operation blocked: '{target}' is outside '{base}'"
        )


# ─────────────────────────────────────────────────────────────
# COPY / SAVE
# ─────────────────────────────────────────────────────────────
def save_file(src_path: str, storage_dir: str) -> str:
    """
    Copy file into storage_dir with timestamp prefix.
    Returns stored file path.
    """
    base = _resolve_storage_dir(storage_dir)

    src = Path(src_path)
    timestamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    dst = base / f"{timestamp}_{src.name}"

    shutil.copy2(src, dst)
    logger.info(f"[file_ops] Saved: {src} → {dst}")
    return str(dst)


# ─────────────────────────────────────────────────────────────
# DELETE
# ─────────────────────────────────────────────────────────────
def delete_file(stored_path: str, storage_dir: str) -> bool:
    """
    Delete file inside storage_dir.
    """
    base = _resolve_storage_dir(storage_dir)
    target = Path(stored_path).resolve()

    _validate_within_directory(target, base)

    if not target.exists():
        logger.warning(f"[file_ops] Delete skipped – not found: {target}")
        return False

    target.unlink()
    logger.info(f"[file_ops] Deleted: {target}")
    return True


# ─────────────────────────────────────────────────────────────
# REPLACE / OVERWRITE
# ─────────────────────────────────────────────────────────────
def replace_file(stored_path: str, new_src_path: str, storage_dir: str) -> str:
    """
    Overwrite stored file with new content.
    """
    base = _resolve_storage_dir(storage_dir)
    target = Path(stored_path).resolve()

    _validate_within_directory(target, base)

    shutil.copy2(new_src_path, target)
    logger.info(f"[file_ops] Replaced: {target} ← {new_src_path}")
    return str(target)


# ─────────────────────────────────────────────────────────────
# WRITE TEXT
# ─────────────────────────────────────────────────────────────
def write_text(stored_path: str, content: str, storage_dir: str) -> str:
    """
    Write text to .txt / .md file.
    """
    base = _resolve_storage_dir(storage_dir)
    target = Path(stored_path).resolve()

    _validate_within_directory(target, base)

    if target.suffix.lower() not in {".txt", ".md"}:
        raise ValueError(
            f"write_text supports only .txt / .md, got: {target.suffix}"
        )

    target.write_text(content, encoding="utf-8")
    logger.info(f"[file_ops] Text written: {target}")
    return str(target)


# ─────────────────────────────────────────────────────────────
# LIST FILES
# ─────────────────────────────────────────────────────────────
def list_stored_files(storage_dir: str) -> list[dict]:
    """
    List all files with metadata.
    """
    base = _resolve_storage_dir(storage_dir)

    entries = []
    for p in sorted(base.iterdir()):
        if p.is_file():
            stat = p.stat()
            entries.append({
                "name": p.name,
                "path": str(p),
                "size_kb": round(stat.st_size / 1024, 2),
                "modified": datetime.datetime.utcfromtimestamp(
                    stat.st_mtime
                ).strftime("%Y-%m-%d %H:%M:%S UTC"),
            })

    return entries


# ─────────────────────────────────────────────────────────────
# EXISTS
# ─────────────────────────────────────────────────────────────
def file_exists(stored_path: str, storage_dir: str) -> bool:
    """
    Check if file exists inside storage_dir.
    """
    base = _resolve_storage_dir(storage_dir)
    target = Path(stored_path).resolve()

    try:
        _validate_within_directory(target, base)
    except ValueError:
        return False

    return target.exists()
