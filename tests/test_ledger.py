import random
from collections import defaultdict

import pytest

from bot import ledger


def people(wallets=(1, 2, 2), weights=("1", "1", "1")):
    return [
        {"id": i + 1, "wallet_id": wallet, "weight": weight}
        for i, (wallet, weight) in enumerate(zip(wallets, weights, strict=True))
    ]


def test_household_two_thirds_and_one_third():
    shares = ledger.split(90000, people(), 1)
    expense = {"payer_wallet_id": 1, "amount_minor": 90000, "shares": shares}
    assert ledger.balances([expense], []) == {1: 60000, 2: -60000}
    expense2 = {"payer_wallet_id": 2, "amount_minor": 120000, "shares": ledger.split(120000, people(), 3)}
    assert ledger.balances([expense2], []) == {1: -40000, 2: 40000}


def test_rounding_and_weights():
    assert [s["amount_minor"] for s in ledger.split(100, people(), 2)] == [33, 34, 33]
    assert ledger.allocate(101, [(2, "1"), (3, "1"), (4, "1")], 1) == {2: 34, 3: 34, 4: 33}
    assert ledger.allocate(100, [(1, "1"), (2, "0.5")], 1) == {1: 67, 2: 33}
    assert [s["amount_minor"] for s in ledger.split(90000, people(), 1, "wallet")] == [45000, 22500, 22500]
    assert sum(s["amount_minor"] for s in ledger.split(1, people(), 1, "wallet")) == 1
    assert [s["amount_minor"] for s in ledger.split(2, people(), 3, "wallet")] == [1, 0, 1]


@pytest.mark.parametrize(
    "amount,weights",
    [
        (0, [(1, "1")]),
        (-1, [(1, "1")]),
        (2, []),
        (2, [(1, "0")]),
        (2, [(1, "-1")]),
        (2, [(1, "1"), (1, "1")]),
        (True, [(1, "1")]),
    ],
)
def test_invalid_splits(amount, weights):
    with pytest.raises(ValueError):
        ledger.allocate(amount, weights)


def test_opening_payment_writeoff_and_undo_signs():
    opening = {"kind": "opening", "from_wallet_id": 1, "to_wallet_id": 2, "amount_minor": 600000}
    payment = {"kind": "payment", "from_wallet_id": 2, "to_wallet_id": 1, "amount_minor": 300000}
    writeoff = {"kind": "writeoff", "from_wallet_id": 2, "to_wallet_id": 1, "amount_minor": 10000}
    assert ledger.balances([], [opening, payment, writeoff]) == {1: 290000, 2: -290000}
    assert ledger.direct([], [opening, payment, writeoff]) == [
        {"from_wallet_id": 2, "to_wallet_id": 1, "amount_minor": 290000}
    ]
    assert ledger.balances([], [opening | {"undone_at": "yes"}]) == {}


def test_randomized_conservation_and_direct_consistency():
    rng = random.Random(321)
    members = people((1, 2, 2, 3, 4), ("1", "0.5", "2", "1", "3"))
    expenses, transfers = [], []
    for _ in range(400):
        chosen = rng.sample(members, rng.randint(1, len(members)))
        chosen.sort(key=lambda m: m["id"])
        payer = rng.choice(members)
        amount = rng.randint(1, 10**8)
        shares = ledger.split(amount, chosen, payer["id"], rng.choice(["person", "wallet"]))
        assert sum(s["amount_minor"] for s in shares) == amount
        assert all(s["amount_minor"] >= 0 for s in shares)
        expenses.append({"payer_wallet_id": payer["wallet_id"], "amount_minor": amount, "shares": shares})
        source, target = rng.sample([1, 2, 3, 4], 2)
        transfers.append(
            {"from_wallet_id": source, "to_wallet_id": target, "amount_minor": rng.randint(1, 10**8)}
        )
        net = ledger.balances(expenses, transfers)
        assert sum(net.values()) == 0
        for lines in (ledger.direct(expenses, transfers), ledger.simplify(net)):
            reconstructed = defaultdict(int)
            for line in lines:
                reconstructed[line["from_wallet_id"]] -= line["amount_minor"]
                reconstructed[line["to_wallet_id"]] += line["amount_minor"]
            assert {w: n for w, n in reconstructed.items() if n} == {w: n for w, n in net.items() if n}
        assert len(ledger.simplify(net)) <= len(net) - 1


def test_summary_ignores_payments_and_deleted_expenses():
    e = {"amount_minor": 100, "created_at": "2026-10-01", "shares": [{"wallet_id": 1, "amount_minor": 100}]}
    assert ledger.summary(
        [e, e | {"deleted_at": "yes"}, e | {"created_at": "2026-09-01"}], [], "2026-10"
    ) == {"month": "2026-10", "total_minor": 100, "wallet_shares": {1: 100}}
