import pytest
from wxread import worker
from wxread.accounts import Account
from wxread.reader import reading_deadline


def account(aid, target):
    return Account(aid, aid, {"wr_vid": aid, "wr_skey": "fixture"}, {}, {"appId": "app", "ps": "p", "pc": "c"}, target_count=target)


def test_default_deadline_allows_the_full_read_and_network_time():
    assert reading_deadline(account("one", 40)) > 1204
    assert reading_deadline(account("one", 200)) > 6000


def test_five_maximum_targets_are_split_below_actions_timeout():
    matrix = worker.read_matrix([account(str(i), 200) for i in range(5)], 30, None)
    ids = []
    for shard in matrix["include"]:
        members = shard["account_ids"].split(",")
        ids.extend(members)
        assert len(members) <= 5
        assert shard["timeout_minutes"] <= 360
    assert sorted(ids) == ["0", "1", "2", "3", "4"]
    assert len(matrix["include"]) > 1


@pytest.mark.parametrize("value", ["nan", "inf", "-1"])
def test_nonfinite_or_negative_read_options_are_rejected(value):
    with pytest.raises(worker.ConfigError):
        worker.read_options({"WXREAD_DEADLINE": value})
