"""Long-path-safe extraction and non-destructive merge of multiple independent Meta ZIP exports."""

from __future__ import annotations

import hashlib
import os
import tempfile
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from meta_archive_pipeline.schema.paths import normalize_rel_path
from meta_archive_pipeline.utils.fs import sha256_file, to_long_path


@dataclass(frozen=True)
class ZipConflict:
    """Record of two ZIP archives containing different bytes at the same relative path."""

    rel_path: str
    source_zip: str
    saved_as: str
    existing_sha256: str
    incoming_sha256: str


@dataclass
class ExtractReport:
    """Summary of a multi-ZIP extraction run."""

    zip_files_processed: int = 0
    files_extracted: int = 0
    duplicates_identical: int = 0
    conflicts: list[ZipConflict] = field(default_factory=list)
    extracted_paths: list[str] = field(default_factory=list)


def _apply_zip_mtime(target: Path, info: zipfile.ZipInfo) -> None:
    try:
        dt = datetime(*info.date_time)
    except ValueError:
        return
    ts = dt.timestamp()
    try:
        os.utime(to_long_path(target), (ts, ts))
    except OSError:
        pass


def _next_conflict_rel_path(dest_dir: Path, rel_path: str) -> tuple[str, Path]:
    rel_p = Path(rel_path)
    parent_rel = rel_p.parent.as_posix()
    stem = rel_p.stem
    suffix = rel_p.suffix
    index = 1
    while True:
        candidate_name = f"{stem}.conflict_{index}{suffix}"
        candidate_rel = (
            candidate_name if parent_rel in ("", ".") else f"{parent_rel}/{candidate_name}"
        )
        candidate_path = dest_dir.joinpath(*candidate_rel.split("/"))
        if not os.path.exists(to_long_path(candidate_path)):
            return candidate_rel, candidate_path
        index += 1


def extract_meta_zips(
    zip_paths: Sequence[Path],
    dest_dir: Path,
    chunk_size: int = 1024 * 1024,
) -> ExtractReport:
    """Extract multiple independent Meta ZIP files into one directory without silent data loss."""
    report = ExtractReport()
    os.makedirs(to_long_path(dest_dir), exist_ok=True)

    for zip_path in sorted(Path(p) for p in zip_paths):
        with zipfile.ZipFile(to_long_path(zip_path), "r") as zf:
            report.zip_files_processed += 1
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if info.filename.startswith(("/", "\\")):
                    raise ValueError(
                        f"Absolute path entry is not allowed in ZIP archive: {info.filename!r}"
                    )
                rel_path = normalize_rel_path(info.filename)
                target = dest_dir.joinpath(*rel_path.split("/"))
                parent_long = to_long_path(target.parent)
                os.makedirs(parent_long, exist_ok=True)

                tmp_path: str | None = None
                digest = hashlib.sha256()
                try:
                    with (
                        zf.open(info, "r") as src,
                        tempfile.NamedTemporaryFile(
                            mode="wb",
                            dir=parent_long,
                            prefix=".extract.",
                            suffix=".tmp",
                            delete=False,
                        ) as tmp,
                    ):
                        tmp_path = tmp.name
                        while chunk := src.read(chunk_size):
                            digest.update(chunk)
                            tmp.write(chunk)
                        tmp.flush()
                        os.fsync(tmp.fileno())

                    incoming_sha = digest.hexdigest()
                    target_long = to_long_path(target)
                    if not os.path.exists(target_long):
                        os.replace(tmp_path, target_long)
                        tmp_path = None
                        _apply_zip_mtime(target, info)
                        report.files_extracted += 1
                        report.extracted_paths.append(rel_path)
                    else:
                        existing_sha = sha256_file(target)
                        if existing_sha == incoming_sha:
                            report.duplicates_identical += 1
                        else:
                            conflict_rel, conflict_path = _next_conflict_rel_path(
                                dest_dir, rel_path
                            )
                            os.replace(tmp_path, to_long_path(conflict_path))
                            tmp_path = None
                            _apply_zip_mtime(conflict_path, info)
                            report.conflicts.append(
                                ZipConflict(
                                    rel_path=rel_path,
                                    source_zip=zip_path.name,
                                    saved_as=conflict_rel,
                                    existing_sha256=existing_sha,
                                    incoming_sha256=incoming_sha,
                                )
                            )
                            report.files_extracted += 1
                            report.extracted_paths.append(conflict_rel)
                finally:
                    if tmp_path is not None and os.path.exists(tmp_path):
                        try:
                            os.remove(tmp_path)
                        except OSError:
                            pass

    return report
