from datetime import date, timedelta

from surge.sources import sec


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeClient:
    """Minimal httpx.Client stand-in returning a canned submissions payload."""
    def __init__(self, payload):
        self._payload = payload

    def get(self, url, headers=None):
        return _Resp(self._payload)


def test_pending_offering_recent():
    recent = (date.today() - timedelta(days=5)).isoformat()
    old = (date.today() - timedelta(days=400)).isoformat()
    payload = {"filings": {"recent": {
        "form": ["S-1", "8-K", "S-3"],
        "filingDate": [recent, recent, old],
    }}}
    res = sec.assess_symbol("ABCD", client=_FakeClient(payload), cik_map={"ABCD": 1})
    assert res["pending_offering"] == 1            # recent S-1
    # both S-1 (recent) and S-3 (old) recorded as offering catalysts
    types = {c[1] for c in res["catalysts"]}
    assert types == {"offering"}
    forms = {c[2] for c in res["catalysts"]}
    assert forms == {"S-1", "S-3"}


def test_no_offering_when_old_only():
    old = (date.today() - timedelta(days=400)).isoformat()
    payload = {"filings": {"recent": {"form": ["S-3"], "filingDate": [old]}}}
    res = sec.assess_symbol("ABCD", client=_FakeClient(payload), cik_map={"ABCD": 1})
    assert res["pending_offering"] == 0
    assert len(res["catalysts"]) == 1             # still recorded, just not "pending"


def test_unknown_ticker_is_noop():
    res = sec.assess_symbol("NOPE", client=_FakeClient({}), cik_map={})
    assert res == {"pending_offering": 0, "catalysts": []}
