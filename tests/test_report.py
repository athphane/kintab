import io
from copy import deepcopy

from PIL import Image

from bot import report


def assert_telegram_image(output):
    assert output.tell() == 0
    assert len(output.getvalue()) < 10 * 1024 * 1024
    with Image.open(output) as image:
        assert image.format == "PNG" and image.mode == "RGB"
        assert image.width == 1080
        assert image.width + image.height < 10000
        assert max(image.size) / min(image.size) < 20
        image.verify()


async def test_report_totals_use_live_entries_not_deleted_or_undone(service, household):
    group, (a, s, h, d) = household
    gid = group["id"]
    await service.add_expense(gid, 11, {"amount_minor": 40000, "description": "Dinner"})
    deleted = await service.add_expense(gid, 11, {"amount_minor": 6000, "description": "Mistake"})
    await service.delete_expense(gid, 11, deleted["id"])
    await service.add_transfer(
        gid, 22, {"amount_minor": 10000, "from_wallet_id": s["wallet_id"], "to_wallet_id": a["wallet_id"]}
    )
    undone = await service.add_transfer(
        gid, 33, {"amount_minor": 1000, "from_wallet_id": h["wallet_id"], "to_wallet_id": a["wallet_id"]}
    )
    await service.undo_transfer(gid, 33, undone["id"])
    await service.add_transfer(
        gid,
        11,
        {
            "kind": "opening",
            "amount_minor": 2000,
            "from_wallet_id": a["wallet_id"],
            "to_wallet_id": d["wallet_id"],
        },
    )
    await service.add_transfer(
        gid,
        11,
        {
            "kind": "writeoff",
            "amount_minor": 1000,
            "from_wallet_id": d["wallet_id"],
            "to_wallet_id": a["wallet_id"],
        },
    )
    state = await service.snapshot(gid)
    summary = report.damage_summary(state)
    assert summary["spent_minor"] == 40000
    assert summary["owed_minor"] == 21000
    assert sum(line["amount_minor"] for line in summary["routes"]) == 21000
    assert_telegram_image(report.damage_image(state))


async def test_direct_mode_cycles_are_still_shown_as_payments(service, household):
    group, (a, s, h, d) = household
    gid = group["id"]
    for uid, source, target in [(11, a, s), (22, s, h), (33, h, a)]:
        await service.add_transfer(
            gid,
            uid,
            {
                "kind": "opening",
                "amount_minor": 100,
                "from_wallet_id": source["wallet_id"],
                "to_wallet_id": target["wallet_id"],
            },
        )
    state = await service.snapshot(gid)
    assert report.damage_summary(state)["owed_minor"] == 0
    await service.update_settings(gid, 11, {"debt_mode": "direct"})
    state = await service.snapshot(gid)
    assert all(balance == 0 for balance in state["balances"].values())
    assert report.damage_summary(state)["owed_minor"] == 300
    assert len(report.damage_summary(state)["routes"]) == 3
    assert_telegram_image(report.damage_image(state))


async def test_settled_empty_and_shared_wallet_reports(service, household):
    group, (a, s, h, d) = household
    gid = group["id"]
    assert_telegram_image(report.damage_image(await service.snapshot(gid)))
    await service.link_wallets(gid, 11, h["wallet_id"], s["wallet_id"])
    summary = report.damage_summary(await service.snapshot(gid))
    assert len(summary["wallets"]) == 3
    await service.leave(gid, 44)
    assert len(report.damage_summary(await service.snapshot(gid))["wallets"]) == 2
    empty_group = await service.ensure_group(-100999, "Fresh start")
    assert_telegram_image(report.damage_image(await service.snapshot(empty_group["id"])))


async def test_large_reports_keep_full_totals_and_disclose_truncation(service, household, monkeypatch):
    group, _ = household
    state = deepcopy(await service.snapshot(group["id"]))
    name = "<House & home> 😀 " * 100
    state["group"]["title"] = name
    creditor = state["wallets"][0]
    creditor["balance_minor"] = 100 * 10**12
    state["members"], state["wallets"], state["lines"] = [], [creditor], []
    for i in range(100):
        state["wallets"].append({"id": 1000 + i, "name": name, "balance_minor": -(10**12)})
        state["lines"].append(
            {"from_wallet_id": 1000 + i, "to_wallet_id": creditor["id"], "amount_minor": 10**12}
        )
    labels = []
    original_text = report._text

    def capture_text(draw, xy, value, *args, **kwargs):
        labels.append(str(value))
        return original_text(draw, xy, value, *args, **kwargs)

    monkeypatch.setattr(report, "_text", capture_text)
    summary = report.damage_summary(state)
    assert summary["owed_minor"] == 100 * 10**12
    assert len(summary["routes"]) == 100 and len(summary["wallets"]) == 101
    output = report.damage_image(state)
    assert_telegram_image(io.BytesIO(output.getvalue()))
    assert "+ 94 more payments in Open tab" in labels
    assert "+ 95 more wallets in Open tab" in labels
