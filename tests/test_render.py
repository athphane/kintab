from copy import deepcopy
from xml.etree import ElementTree

from bot import render


async def test_undo_and_delete_keep_fallback_running_total(service, household):
    group, (a, s, _, _) = household
    expense = await service.add_expense(group["id"], 11, {"amount_minor": 40000, "description": "Groceries"})
    payment = await service.add_transfer(
        group["id"],
        22,
        {"from_wallet_id": s["wallet_id"], "to_wallet_id": a["wallet_id"], "amount_minor": 10000},
    )
    undone = await service.undo_transfer(group["id"], 22, payment["id"])
    state = await service.snapshot(group["id"])
    post = render.entry_text(state, "transfer", undone)
    assert "<s>" in post and "Undone by" in post and "Running total:" in post
    assert post.split("Running total:", 1)[1].count("<b>100.00</b>") == 3
    deleted = await service.delete_expense(group["id"], 11, expense["id"])
    state = await service.snapshot(group["id"])
    post = render.entry_text(state, "expense", deleted)
    assert "Deleted by" in post and "Running total:" in post and "All settled" in post


async def test_large_group_posts_remain_valid_bounded_html(service, household):
    group, (a, s, _, _) = household
    expense = await service.add_expense(group["id"], 11, {"amount_minor": 400, "description": "'" * 200})
    state = await service.snapshot(group["id"])
    state["group"]["title"] = "'" * 200
    state["members"] = [m | {"name": "'😀" * 50} for m in state["members"]]
    for i in range(150):
        state["members"].append(deepcopy(state["members"][0]) | {"id": i + 100})
        state["wallets"].append({"id": i + 100, "name": "'😀" * 100, "settler": state["members"][0]})
        state["lines"].append({"from_wallet_id": i + 100, "to_wallet_id": a["wallet_id"], "amount_minor": 1})
        expense["shares"].append({"member_id": i + 100, "wallet_id": i + 100, "amount_minor": 1})
    for content in (
        render.welcome(state),
        render.running(state),
        render.entry_text(state, "expense", expense),
    ):
        assert len(content.encode("utf-16-le")) // 2 < 4096
        assert "more" in content
        # Generated HTML has complete tags even when rows/names are shortened.
        ElementTree.fromstring("<root>" + content + "</root>")
