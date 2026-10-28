from datetime import UTC, datetime, timedelta

import pytest

from bot import parse, perms


@pytest.mark.parametrize(
    "source,result", [("450", 45000), ("1,200", 120000), ("1200.50", 120050), ("0.01", 1)]
)
def test_money(source, result):
    assert parse.amount_minor(source) == result


@pytest.mark.parametrize("source", ["1,20", "1,200,0", "-3", "0", "NaN", "1e2", "1.001", ".5", "5.", "١٢"])
def test_bad_money(source):
    with pytest.raises(ValueError):
        parse.amount_minor(source)


def test_parsing_description_and_mention():
    parsed = parse.parse_command("/spent@KinTabBot 1,200.50 electricity @Ibrahim")
    assert (parsed.amount_minor, parsed.description, parsed.username) == (120050, "electricity", "ibrahim")
    assert parse.amount_minor("450", 0) == 450
    assert parse.amount_minor("1.234", 3) == 1234


def test_wallet_and_logger_permissions():
    e = {"payer_wallet_id": 2, "logger_member_id": 1, "shares": [{"member_id": 4}]}
    assert perms.expense_edit({"id": 3, "wallet_id": 2}, e)
    assert perms.expense_edit({"id": 1, "wallet_id": 9}, e)
    assert not perms.expense_edit({"id": 4, "wallet_id": 3}, e)
    assert perms.expense_review({"id": 4}, e)
    assert not perms.expense_review({"id": 5}, e)


def test_chat_payment_undo_window_and_opening_exception():
    clock = datetime.now(UTC)
    payment = {
        "from_wallet_id": 1,
        "to_wallet_id": 2,
        "kind": "payment",
        "created_at": (clock - timedelta(hours=25)).isoformat(),
    }
    assert not perms.transfer_edit({"id": 1, "wallet_id": 1}, payment, chat=True, now=clock)
    assert perms.transfer_edit({"id": 2, "wallet_id": 2}, payment)
    assert not perms.transfer_edit({"id": 3, "wallet_id": 3}, payment)
    assert perms.transfer_edit({"id": 1, "wallet_id": 1}, payment | {"kind": "opening"}, chat=True, now=clock)
