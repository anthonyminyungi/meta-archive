"""Tests for atomic JSON checkpoint store."""

from __future__ import annotations

from pathlib import Path

from meta_archive_pipeline.utils.checkpoint import CheckpointStore


def test_checkpoint_lifecycle_and_reload(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "state.json"
    store = CheckpointStore(state_path)

    added = store.ensure_items(["post_1", "post_2", "post_3"])
    assert added == 3
    assert store.ensure_items(["post_2", "post_4"]) == 1

    assert store.get_status("post_1") == "pending"
    assert store.get_status("missing") is None
    assert store.pending_ids() == ["post_1", "post_2", "post_3", "post_4"]

    store.set_status("post_1", "fetched", meta={"shortcode": "ABC"})
    store.set_status("post_2", "media_saved")
    store.set_status("post_3", "done")
    store.set_status("post_4", "failed", error="HTTP 429 rate limited")

    assert store.ids_by_status("done") == ["post_3"]
    assert store.ids_by_status("failed") == ["post_4"]
    assert store.pending_ids() == ["post_1", "post_2", "post_4"]
    assert store.pending_ids(include_in_progress=False) == []
    assert store.summary() == {
        "pending": 0,
        "fetched": 1,
        "media_saved": 1,
        "done": 1,
        "failed": 1,
    }

    # Reload from disk and verify full persistence + no leftover .tmp files
    reloaded = CheckpointStore(state_path)
    assert reloaded.get_status("post_1") == "fetched"
    assert reloaded.get_record("post_1").meta == {"shortcode": "ABC"}
    assert reloaded.get_record("post_4").error == "HTTP 429 rate limited"
    assert reloaded.summary() == store.summary()

    tmp_leftovers = [p for p in state_path.parent.iterdir() if p.name.endswith(".tmp")]
    assert tmp_leftovers == []
