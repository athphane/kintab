"""Pure ledger operations. All monetary values are integer minor units."""

from collections import defaultdict
from fractions import Fraction

MAX_MINOR = 10**12


def positive_amount(value: int) -> int:
    if type(value) is not int or not 0 < value <= MAX_MINOR:
        raise ValueError("Amount must be a positive integer in minor units (at most 1 trillion).")
    return value


def allocate(amount: int, weighted: list[tuple[int, str]], preferred: int | None = None) -> dict[int, int]:
    """Floor weighted shares; give remaining minor units to payer, else join order."""
    positive_amount(amount)
    if not weighted or len({key for key, _ in weighted}) != len(weighted):
        raise ValueError("Choose at least one distinct participant.")
    weights = [(key, Fraction(str(weight))) for key, weight in weighted]
    if any(weight <= 0 or weight > 1000 for _, weight in weights):
        raise ValueError("Weights must be greater than zero and at most 1000.")
    total = sum(weight for _, weight in weights)
    shares = {key: int(amount * weight / total) for key, weight in weights}
    remainder = amount - sum(shares.values())
    if preferred in shares:
        shares[preferred] += remainder
    else:
        for key, _ in weights[:remainder]:
            shares[key] += 1
    return shares


def split(
    amount: int,
    members: list[dict],
    payer_id: int,
    mode: str = "person",
    weights: dict[int, str] | None = None,
) -> list[dict]:
    """Members must arrive in join order. Custom weights always mean per person."""
    if mode not in {"person", "wallet"}:
        raise ValueError("Unknown split mode.")
    if weights is not None and set(weights) != {m["id"] for m in members}:
        raise ValueError("Custom weights must cover exactly the selected people.")
    if mode == "person" or weights is not None:
        amounts = allocate(
            amount, [(m["id"], (weights or {}).get(m["id"], m["weight"])) for m in members], payer_id
        )
    else:
        wallets = list(dict.fromkeys(m["wallet_id"] for m in members))
        payer_wallet = next((m["wallet_id"] for m in members if m["id"] == payer_id), None)
        wallet_amounts = allocate(amount, [(w, "1") for w in wallets], payer_wallet)
        amounts = {}
        for wallet_id, part in wallet_amounts.items():
            people = [m for m in members if m["wallet_id"] == wallet_id]
            amounts.update(
                allocate(part, [(m["id"], m["weight"]) for m in people], payer_id)
                if part
                else {m["id"]: 0 for m in people}
            )
    return [
        {"member_id": m["id"], "wallet_id": m["wallet_id"], "amount_minor": amounts[m["id"]]} for m in members
    ]


def balances(expenses: list[dict], transfers: list[dict]) -> dict[int, int]:
    net = defaultdict(int)
    for expense in expenses:
        if expense.get("deleted_at"):
            continue
        net[expense["payer_wallet_id"]] += expense["amount_minor"]
        for share in expense["shares"]:
            net[share["wallet_id"]] -= share["amount_minor"]
    for transfer in transfers:
        if not transfer.get("undone_at"):
            net[transfer["from_wallet_id"]] += transfer["amount_minor"]
            net[transfer["to_wallet_id"]] -= transfer["amount_minor"]
    return dict(net)


def simplify(net: dict[int, int]) -> list[dict]:
    if sum(net.values()) != 0:
        raise ValueError("Balances must sum to zero.")
    debtors = {w: -n for w, n in net.items() if n < 0}
    creditors = {w: n for w, n in net.items() if n > 0}
    result = []
    while debtors:
        source = max(debtors, key=lambda w: (debtors[w], -w))
        target = max(creditors, key=lambda w: (creditors[w], -w))
        amount = min(debtors[source], creditors[target])
        result.append({"from_wallet_id": source, "to_wallet_id": target, "amount_minor": amount})
        debtors[source] -= amount
        creditors[target] -= amount
        if not debtors[source]:
            del debtors[source]
        if not creditors[target]:
            del creditors[target]
    return result


def direct(expenses: list[dict], transfers: list[dict]) -> list[dict]:
    pairs = defaultdict(int)

    def add(source, target, amount):
        if source != target:
            pairs[min(source, target), max(source, target)] += amount if source < target else -amount

    for expense in expenses:
        if not expense.get("deleted_at"):
            for share in expense["shares"]:
                add(share["wallet_id"], expense["payer_wallet_id"], share["amount_minor"])
    for transfer in transfers:
        if not transfer.get("undone_at"):
            add(transfer["from_wallet_id"], transfer["to_wallet_id"], -transfer["amount_minor"])
    return [
        {"from_wallet_id": a if n > 0 else b, "to_wallet_id": b if n > 0 else a, "amount_minor": abs(n)}
        for (a, b), n in sorted(pairs.items())
        if n
    ]


def summary(expenses: list[dict], members: list[dict], month: str) -> dict:
    shares = defaultdict(int)
    total = 0
    for expense in expenses:
        if not expense.get("deleted_at") and expense["created_at"].startswith(month):
            total += expense["amount_minor"]
            for share in expense["shares"]:
                shares[share["wallet_id"]] += share["amount_minor"]
    return {"month": month, "total_minor": total, "wallet_shares": dict(shares)}
