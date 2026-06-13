"""award_satisfies must accept RA's real award spellings — RA returns "beaten-hardcore"
for a hardcore beat (not plain "beaten"), which previously left hardcore-beaten goals
stuck active forever."""
from app.services.goals import award_satisfies


def test_beaten_hardcore_satisfies_beaten():
    # The regression: RA's actual hardcore-beaten kind.
    assert award_satisfies("beaten", "beaten-hardcore") is True


def test_plain_beaten_and_mastered_satisfy_beaten():
    assert award_satisfies("beaten", "beaten") is True
    assert award_satisfies("beaten", "mastered") is True


def test_softcore_never_satisfies_beaten():
    assert award_satisfies("beaten", "beaten-softcore") is False
    assert award_satisfies("beaten", "completed") is False
    assert award_satisfies("beaten", "") is False


def test_master_requires_hardcore_mastered():
    assert award_satisfies("master", "mastered") is True
    assert award_satisfies("master", "beaten-hardcore") is False
    assert award_satisfies("master", "completed") is False
