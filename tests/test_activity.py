"""Per-card activity overlay: a finished item drops its spinner while the batch
runs on (the 'done cards still show Hashing' bug)."""

from app.services import activity


def test_complete_entry_drops_finished_card_overlay():
    activity._tasks.clear()
    activity.start_batch("rehash-batch", "Hashing 3 ROMs", 3, "rehash", entry_ids=[1, 2, 3])

    # All three start with the overlay.
    assert activity.get_card_states()["states"] == {
        "lib-1": "rehash", "lib-2": "rehash", "lib-3": "rehash",
    }

    # Finishing one drops only that card's overlay; the others keep spinning.
    activity.complete_entry("rehash-batch", 1)
    states = activity.get_card_states()["states"]
    assert "lib-1" not in states
    assert states == {"lib-2": "rehash", "lib-3": "rehash"}

    # ...and progress still advances.
    assert activity._tasks["rehash-batch"].completed == 1
    activity._tasks.clear()
