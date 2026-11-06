from zeitnot.ledger import SpendLedger


def test_ledger_persists_across_instances(tmp_path):
    path = tmp_path / "data" / "spend.json"
    ledger = SpendLedger(path)
    ledger.add(0.25)
    ledger.add(0.5)
    assert SpendLedger(path).spent_usd == 0.75


def test_ledger_without_a_path_is_in_memory():
    ledger = SpendLedger(None)
    ledger.add(1.0)
    assert ledger.spent_usd == 1.0
