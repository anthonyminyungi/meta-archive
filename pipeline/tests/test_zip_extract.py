"""Tests for long-path-safe multi-ZIP extraction and conflict handling."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from meta_archive_pipeline.utils.fs import to_long_path
from meta_archive_pipeline.utils.zip_extract import extract_meta_zips


def _write_zip(zip_path: Path, entries: dict[str, bytes]) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, payload in entries.items():
            info = zipfile.ZipInfo(name, date_time=(2024, 6, 15, 10, 30, 0))
            zf.writestr(info, payload)


def test_merge_multiple_zips_with_identical_duplicates_and_conflicts(tmp_path: Path) -> None:
    zip1 = tmp_path / "export-1.zip"
    zip2 = tmp_path / "export-2.zip"
    dest = tmp_path / "extracted"

    _write_zip(
        zip1,
        {
            "your_instagram_activity/media/posts_1.json": b'{"ok": 1}',
            "media/posts/shared.jpg": b"same-bytes",
            "media/posts/changed.jpg": b"version-one",
        },
    )
    _write_zip(
        zip2,
        {
            "media/posts/shared.jpg": b"same-bytes",
            "media/posts/changed.jpg": b"version-two",
            "media/stories/s1.jpg": b"story-bytes",
        },
    )

    report = extract_meta_zips([zip1, zip2], dest)

    assert report.zip_files_processed == 2
    assert report.duplicates_identical == 1
    assert report.files_extracted == 5
    assert len(report.extracted_paths) == 5
    assert len(report.conflicts) == 1

    conflict = report.conflicts[0]
    assert conflict.rel_path == "media/posts/changed.jpg"
    assert conflict.source_zip == "export-2.zip"
    assert conflict.saved_as == "media/posts/changed.conflict_1.jpg"

    assert (dest / "your_instagram_activity/media/posts_1.json").read_bytes() == b'{"ok": 1}'
    assert (dest / "media/posts/shared.jpg").read_bytes() == b"same-bytes"
    assert (dest / "media/posts/changed.jpg").read_bytes() == b"version-one"
    assert (dest / "media/posts/changed.conflict_1.jpg").read_bytes() == b"version-two"
    assert (dest / "media/stories/s1.jpg").read_bytes() == b"story-bytes"


def test_long_path_over_260_chars_lossless(tmp_path: Path) -> None:
    deep_rel = "/".join(["very_long_segment_" + "a" * 35 for _ in range(7)]) + "/deep_file.json"
    zip_file = tmp_path / "long.zip"
    payload = b'{"nested": "long-path-ok"}'
    _write_zip(zip_file, {deep_rel: payload})

    dest = tmp_path / "out"
    target = dest.joinpath(*deep_rel.split("/"))
    assert len(str(target)) > 260

    report = extract_meta_zips([zip_file], dest)
    assert report.files_extracted == 1
    with open(to_long_path(target), "rb") as handle:
        assert handle.read() == payload


@pytest.mark.parametrize(
    "bad_member",
    [
        "../outside.txt",
        "media/../../escape.txt",
        "/etc/passwd",
        r"C:\Windows\system.ini",
    ],
)
def test_zip_slip_and_absolute_paths_rejected(tmp_path: Path, bad_member: str) -> None:
    zip_file = tmp_path / "bad.zip"
    _write_zip(zip_file, {bad_member: b"evil"})

    with pytest.raises(ValueError):
        extract_meta_zips([zip_file], tmp_path / "dest")
