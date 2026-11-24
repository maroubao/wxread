from wxread import control
from wxread.accounts import account_from_curl, Account
from wxread.jobs import JobRunner
from support import CURL_SAMPLE
from test_reader import FakeTransport, ok


def test_stop_is_bound_to_the_requested_run(store):
    account = account_from_curl(CURL_SAMPLE)
    data = account.to_dict()
    data["progress"] = {"run_id": "current", "status": "running", "read_count": 1}
    store.save_account(data)
    context = control.ControlContext(store)
    import pytest
    with pytest.raises(control.ControlError):
        control.handle_stop(context, {"account_id": account.id, "run_id": "old"})
    control.handle_stop(context, {"account_id": account.id, "run_id": "current"})
    assert store.load_account(account.id)["progress"]["cancel_requested"] is True


def test_stopping_one_run_does_not_prevent_a_later_run(store):
    account = account_from_curl(CURL_SAMPLE)
    account.target_count = 2
    store.save_account(account)

    class StopAfterFirst(FakeTransport):
        def post(self, *args):
            raw = store.load_account(account.id)
            control.handle_stop(control.ControlContext(store), {"account_id": account.id, "run_id": raw["progress"]["run_id"]})
            return ok()

    first = JobRunner(store, transport_factory=StopAfterFirst, pace=0).run([account])[0]
    assert first["read_count"] == 1 and first["reason"] == "cancelled"
    fresh = Account.from_dict(store.load_account(account.id))
    second = JobRunner(store, transport_factory=lambda: FakeTransport([ok(), ok()]), pace=0).run([fresh])[0]
    assert second["read_count"] == 2
