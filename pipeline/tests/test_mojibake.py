"""Tests for Meta Latin-1/UTF-8 mojibake recovery."""

from __future__ import annotations

import copy

import pytest

from meta_archive_pipeline.utils.mojibake import fix_mojibake, fix_mojibake_tree


def _to_meta_mojibake(text: str) -> str:
    return text.encode("utf-8").decode("latin-1")


def test_korean_mojibake_recovery() -> None:
    original = "제주도 여행 첫날 🌅 #바다 #추억"
    garbled = _to_meta_mojibake(original)
    assert garbled != original
    assert fix_mojibake(garbled) == original


def test_compound_zwj_emoji_mojibake_recovery() -> None:
    original = "가족 나들이 👨‍👩‍👧‍👦🎉 좋아요 👍🏽"
    garbled = _to_meta_mojibake(original)
    assert fix_mojibake(garbled) == original


def test_cesu8_paired_surrogate_emoji_recovery() -> None:
    # Old Meta exports occasionally encode 4-byte emoji as two 3-byte UTF-8 surrogates.
    cesu8_garbled = b"\xed\xa0\xbd\xed\xb8\x80".decode("latin-1")
    assert fix_mojibake(cesu8_garbled) == "😀"

    mixed_cesu8 = _to_meta_mojibake("안녕 ") + cesu8_garbled
    assert fix_mojibake(mixed_cesu8) == "안녕 😀"


def test_native_utf8_preserved() -> None:
    native = "정상 UTF-8 한글과 이모지 😊✨"
    assert fix_mojibake(native) == native
    assert fix_mojibake("") == ""
    assert fix_mojibake("plain ascii 123!") == "plain ascii 123!"


def test_mixed_native_and_mojibake_string() -> None:
    mixed = "정상prefix " + _to_meta_mojibake("깨진부분🔥") + " 정상suffix"
    assert fix_mojibake(mixed) == "정상prefix 깨진부분🔥 정상suffix"


@pytest.mark.parametrize("word", ["café", "naïve", "résumé", "über"])
def test_genuine_latin1_accented_words_not_corrupted(word: str) -> None:
    assert fix_mojibake(word) == word


def test_fix_mojibake_tree_non_mutating() -> None:
    original_tree = {
        _to_meta_mojibake("작성자"): _to_meta_mojibake("윤기"),
        "comments": [
            {"text": _to_meta_mojibake("반갑습니다 🙌")},
            {"text": "이미 정상 한글 café"},
        ],
        "meta": (_to_meta_mojibake("태그"), 42, True, None),
    }
    snapshot = copy.deepcopy(original_tree)

    fixed = fix_mojibake_tree(original_tree)
    assert original_tree == snapshot
    assert fixed == {
        "작성자": "윤기",
        "comments": [
            {"text": "반갑습니다 🙌"},
            {"text": "이미 정상 한글 café"},
        ],
        "meta": ("태그", 42, True, None),
    }
