import asyncio
import csv
import io
import json

import pytest

from bot.perms import PermissionDenied


async def test_link_consent_shared_permissions_and_snapshots(service, household):
    g, (a, s, h, d) = household
    gid = g["id"]
    invite = await service.invite(gid, 22, h["id"])
    with pytest.raises(PermissionDenied):
        await service.respond_invite(gid, 11, invite["id"], True)
    await service.respond_invite(gid, 33, invite["id"], True)
    state = await service.snapshot(gid, 33)
    assert state["me"]["wallet_id"] == s["wallet_id"]
    assert next(w for w in state["wallets"] if w["id"] == s["wallet_id"])["settler_member_id"] == s["id"]
    await service.leave(gid, 44)
    entry = await service.add_expense(gid, 11, {"amount_minor": 90000, "description": "Groceries"})
    state = await service.snapshot(gid)
    assert state["balances"][a["wallet_id"]] == 60000
    assert state["balances"][s["wallet_id"]] == -60000
    with pytest.raises(ValueError):
        await service.unlink(gid, 33)
    with pytest.raises(ValueError):
        await service.invite(gid, 11, s["id"])
    with pytest.raises(PermissionDenied):
        await service.delete_expense(gid, 33, entry["id"])
    husband_expense = await service.add_expense(
        gid, 33, {"amount_minor": 120000, "description": "Electricity"}
    )
    await service.edit_expense(gid, 22, husband_expense["id"], {"amount_minor": 90000})
    await service.delete_expense(gid, 22, husband_expense["id"])
    payment = await service.add_transfer(
        gid, 33, {"amount_minor": 60000, "from_wallet_id": s["wallet_id"], "to_wallet_id": a["wallet_id"]}
    )
    assert all(v == 0 for v in (await service.snapshot(gid))["balances"].values())
    await service.undo_transfer(gid, 22, payment["id"])
    assert (await service.snapshot(gid))["balances"][s["wallet_id"]] == -60000


async def test_leave_late_join_and_no_debt_redistribution(service, household):
    g, (a, s, h, d) = household
    gid = g["id"]
    original = await service.add_expense(gid, 11, {"amount_minor": 10000, "description": "Old"})
    await service.leave(gid, 44)
    newcomer = await service.join(gid, 55, "Late")
    new = await service.add_expense(gid, 11, {"amount_minor": 10000, "description": "New"})
    assert d["id"] not in [s["member_id"] for s in new["shares"]]
    assert newcomer["id"] not in [s["member_id"] for s in original["shares"]]
    state = await service.snapshot(gid)
    assert state["balances"][d["wallet_id"]] == -2500
    assert next(w for w in state["wallets"] if w["id"] == d["wallet_id"])["name"] == "Dan (left)"
    # Editing an old expense preserves its historical people and wallets.
    edited = await service.edit_expense(gid, 11, original["id"], {"amount_minor": 20000})
    assert d["id"] in [s["member_id"] for s in edited["shares"]]
    assert newcomer["id"] not in [s["member_id"] for s in edited["shares"]]
    with pytest.raises(PermissionDenied):
        await service.snapshot(gid, 44)
    await service.join(gid, 44, "Dan", "dan")
    assert (await service.snapshot(gid, 44))["me"]["wallet_id"] == d["wallet_id"]


async def test_writeoff_opening_and_permissions(service, household):
    g, (a, s, h, d) = household
    gid = g["id"]
    opening = await service.add_transfer(
        gid,
        11,
        {
            "kind": "opening",
            "amount_minor": 600000,
            "from_wallet_id": a["wallet_id"],
            "to_wallet_id": s["wallet_id"],
        },
    )
    with pytest.raises(PermissionDenied):
        await service.undo_transfer(gid, 44, opening["id"])
    with pytest.raises(PermissionDenied):
        await service.add_transfer(
            gid,
            22,
            {
                "kind": "writeoff",
                "amount_minor": 100,
                "from_wallet_id": s["wallet_id"],
                "to_wallet_id": a["wallet_id"],
            },
        )
    with pytest.raises(ValueError):
        await service.add_transfer(
            gid,
            11,
            {
                "kind": "writeoff",
                "amount_minor": 600001,
                "from_wallet_id": s["wallet_id"],
                "to_wallet_id": a["wallet_id"],
            },
        )
    writeoff = await service.add_transfer(
        gid,
        11,
        {
            "kind": "writeoff",
            "amount_minor": 600000,
            "from_wallet_id": s["wallet_id"],
            "to_wallet_id": a["wallet_id"],
        },
    )
    assert all(n == 0 for n in (await service.snapshot(gid))["balances"].values())
    await service.undo_transfer(gid, 22, writeoff["id"])
    await service.edit_opening(gid, 22, opening["id"], {"amount_minor": 100})
    assert (await service.snapshot(gid))["balances"][a["wallet_id"]] == 100


async def test_atomic_validation_audit_and_duplicate_commands(service, household):
    g, people = household
    gid = g["id"]
    with pytest.raises(ValueError):
        await service.add_expense(gid, 11, {"amount_minor": 10, "member_ids": []}, 123)
    entry = await service.add_expense(gid, 11, {"amount_minor": 10}, 123)
    with pytest.raises(ValueError):
        await service.add_expense(gid, 11, {"amount_minor": 10}, 123)
    await service.edit_expense(gid, 11, entry["id"], {"amount_minor": 20, "revision": 1})
    with pytest.raises(ValueError):
        await service.edit_expense(gid, 11, entry["id"], {"amount_minor": 30, "revision": 1})
    async with service.db.transaction():
        audit = await service.db.all(
            "SELECT * FROM audit_log WHERE entity='expense' AND entity_id=?", (entry["id"],)
        )
    assert [a["action"] for a in audit] == ["create", "edit"]
    assert '"amount_minor": 10' in audit[1]["before_json"]
    assert '"amount_minor": 20' in audit[1]["after_json"]
    await asyncio.gather(*(service.add_expense(gid, 11, {"amount_minor": 1}) for _ in range(20)))
    assert sum((await service.snapshot(gid))["balances"].values()) == 0


async def test_cross_group_injection(service, household):
    g, people = household
    other = await service.ensure_group(-456, "Other")
    outsider = await service.join(other["id"], 11, "Same user")
    with pytest.raises(ValueError):
        await service.add_expense(g["id"], 11, {"amount_minor": 100, "payer_member_id": outsider["id"]})
    with pytest.raises(ValueError):
        await service.add_expense(g["id"], 11, {"amount_minor": 100, "member_ids": [outsider["id"]]})
    with pytest.raises(ValueError):
        await service.add_transfer(
            g["id"],
            11,
            {
                "amount_minor": 100,
                "from_wallet_id": people[0]["wallet_id"],
                "to_wallet_id": outsider["wallet_id"],
            },
        )
    entry = await service.add_expense(other["id"], 11, {"amount_minor": 100})
    with pytest.raises(ValueError):
        await service.delete_expense(g["id"], 11, entry["id"])


async def test_settings_guests_migration_export_and_forget(service, household):
    g, people = household
    gid = g["id"]
    guest = await service.add_guest(gid, 11, "Parent")
    await service.update_settings(gid, 22, {"debt_mode": "direct"})
    await service.update_settings(gid, 11, {"split_mode": "wallet", "weights": {str(people[0]["id"]): "0.5"}})
    await service.add_expense(
        gid, 11, {"amount_minor": 100, "description": "=unsafe formula", "payer_member_id": guest["id"]}
    )
    with pytest.raises(ValueError):
        await service.update_settings(gid, 11, {"currency": "USD"})
    before = (await service.snapshot(gid))["group"]["split_mode"]
    with pytest.raises(ValueError):
        await service.update_settings(gid, 11, {"split_mode": "person", "weights": {str(guest["id"]): "-1"}})
    assert (await service.snapshot(gid))["group"]["split_mode"] == before
    await service.migrate(g["chat_id"], -10098765)
    assert (await service.snapshot(gid))["group"]["chat_id"] == -10098765
    assert (await service.group_for_token(g["token"]))["id"] == gid
    rows = list(csv.DictReader(io.StringIO(await service.export_csv(gid, 11))))
    assert rows[0]["description"] == "'=unsafe formula"
    assert any(r["entity"] == "audit" for r in rows)
    with pytest.raises(PermissionDenied):
        await service.forget(gid, 11, confirmation="wrong")
    with pytest.raises(PermissionDenied):
        await service.forget(gid, 999, confirmation=g["token"])
    await service.forget(gid, 22, confirmation=g["token"])
    assert await service.group_for_token(g["token"]) is None
    async with service.db.transaction():
        for table in ["members", "wallets", "expenses", "transfers", "expense_shares", "audit_log"]:
            assert (await service.db.one(f"SELECT COUNT(*) AS count FROM {table}"))["count"] == 0
    fresh = await service.ensure_group(g["chat_id"], "New")
    assert fresh["id"] != gid


async def test_custom_split_and_shared_wallet_leave(service, household):
    g, (a, s, h, d) = household
    gid = g["id"]
    invite = await service.invite(gid, 22, h["id"])
    await service.respond_invite(gid, 33, invite["id"], True)
    entry = await service.add_expense(
        gid,
        11,
        {
            "amount_minor": 100,
            "member_ids": [a["id"], s["id"]],
            "weights": {str(a["id"]): "1", str(s["id"]): "0.5"},
        },
    )
    assert [s["amount_minor"] for s in entry["shares"]] == [67, 33]
    await service.leave(gid, 22)
    state = await service.snapshot(gid, 33)
    assert state["me"]["wallet_id"] == s["wallet_id"]
    assert state["balances"][s["wallet_id"]] == -33
    assert next(w for w in state["wallets"] if w["id"] == s["wallet_id"])["settler_member_id"] == h["id"]
    await service.review_expense(gid, 11, entry["id"])
    with pytest.raises(PermissionDenied):
        await service.review_expense(gid, 44, entry["id"])


async def test_historical_wallet_not_rewritten_and_can_be_settled(service, household):
    group, (a, s, h, d) = household
    gid = group["id"]
    expense = await service.add_expense(gid, 11, {"amount_minor": 400, "payer_member_id": h["id"]})
    for person, uid in [(a, 11), (s, 22), (d, 44)]:
        await service.add_transfer(
            gid,
            uid,
            {"amount_minor": 100, "from_wallet_id": person["wallet_id"], "to_wallet_id": h["wallet_id"]},
        )
    assert all(value == 0 for value in (await service.snapshot(gid))["balances"].values())
    invite = await service.invite(gid, 22, h["id"])
    await service.respond_invite(gid, 33, invite["id"], True)
    after = await service.edit_expense(gid, 22, expense["id"], {"amount_minor": 800})
    assert after["payer_wallet_id"] == h["wallet_id"]
    assert [s["wallet_id"] for s in after["shares"]] == [s["wallet_id"] for s in expense["shares"]]
    assert (await service.snapshot(gid, 22))["me"]["wallet_ids"] == [s["wallet_id"], h["wallet_id"]]
    # The historical creditor can receive settlement through either partner.
    await service.add_transfer(
        gid, 22, {"amount_minor": 100, "from_wallet_id": d["wallet_id"], "to_wallet_id": h["wallet_id"]}
    )


async def test_invitation_cannot_accept_a_different_wallet(service, household):
    group, (a, s, h, d) = household
    gid = group["id"]
    invitation = await service.invite(gid, 22, h["id"])
    second = await service.invite(gid, 11, h["id"])
    await service.respond_invite(gid, 33, second["id"], True)
    with pytest.raises(ValueError, match="wallets changed"):
        await service.respond_invite(gid, 33, invitation["id"], True)


async def test_member_manages_other_wallets_and_guests_with_audit(service, household):
    group, (admin, sister, husband, dan) = household
    gid = group["id"]
    guest = await service.add_guest(gid, 44, "Child")
    invite = await service.invite(gid, 44, husband["id"])
    await service.link_wallets(gid, 44, husband["wallet_id"], sister["wallet_id"])
    await service.link_wallets(gid, 44, guest["wallet_id"], sister["wallet_id"])
    state = await service.snapshot(gid, 44)
    assert state["me"]["wallet_id"] == dan["wallet_id"]
    assert {m["id"] for m in state["members"] if m["wallet_id"] == sister["wallet_id"]} == {
        sister["id"],
        husband["id"],
        guest["id"],
    }
    wallet = next(w for w in state["wallets"] if w["id"] == sister["wallet_id"])
    assert wallet["settler_member_id"] == sister["id"]
    with pytest.raises(ValueError, match="no longer pending"):
        await service.respond_invite(gid, 33, invite["id"], True)
    await service.set_settler(gid, 44, husband["id"])
    await service.unlink(gid, 44, husband["id"])
    await service.unlink(gid, 44, guest["id"])
    state = await service.snapshot(gid)
    for person in (husband, guest):
        member = next(m for m in state["members"] if m["id"] == person["id"])
        assert member["wallet_id"] not in {person["wallet_id"], sister["wallet_id"]}
        assert (
            next(w for w in state["wallets"] if w["id"] == member["wallet_id"])["settler_member_id"]
            == person["id"]
        )
    assert (
        next(w for w in state["wallets"] if w["id"] == sister["wallet_id"])["settler_member_id"]
        == sister["id"]
    )
    async with service.db.transaction():
        audit = await service.db.all(
            "SELECT * FROM audit_log WHERE action IN ('link','unlink','settler') ORDER BY id"
        )
    assert all(row["member_id"] == dan["id"] for row in audit)
    assert [row["action"] for row in audit] == ["link", "link", "settler", "settler", "unlink", "unlink"]
    assert json.loads(audit[-1]["before_json"])["wallet_id"] == sister["wallet_id"]
    assert json.loads(audit[-1]["after_json"])["wallet_id"] != sister["wallet_id"]


async def test_wallet_membership_and_validation_are_atomic(service, household):
    group, (admin, sister, husband, dan) = household
    gid = group["id"]
    before = await service.snapshot(gid)
    for user_id in (999, 55):
        with pytest.raises(PermissionDenied):
            await service.link_wallets(gid, user_id, husband["wallet_id"], sister["wallet_id"])
        with pytest.raises(PermissionDenied):
            await service.unlink(gid, user_id, husband["id"])
        with pytest.raises(PermissionDenied):
            await service.set_settler(gid, user_id, husband["id"])
    with pytest.raises(ValueError, match="different wallets"):
        await service.link_wallets(gid, 11, husband["wallet_id"], husband["wallet_id"])
    assert await service.snapshot(gid) == before
    other_group = await service.ensure_group(-100456, "Other")
    outsider = await service.join(other_group["id"], 55, "Other member")
    with pytest.raises(ValueError, match="not found"):
        await service.link_wallets(gid, 11, outsider["wallet_id"], sister["wallet_id"])
    with pytest.raises(ValueError, match="not found"):
        await service.link_wallets(gid, 11, sister["wallet_id"], outsider["wallet_id"])
    with pytest.raises(ValueError):
        await service.unlink(gid, 11, outsider["id"])
    with pytest.raises(ValueError):
        await service.set_settler(gid, 11, outsider["id"])
    await service.link_wallets(gid, 11, husband["wallet_id"], sister["wallet_id"])
    with pytest.raises(ValueError, match="active members"):
        await service.link_wallets(gid, 11, husband["wallet_id"], sister["wallet_id"])
    await service.leave(gid, 44)
    with pytest.raises(PermissionDenied):
        await service.link_wallets(gid, 44, sister["wallet_id"], admin["wallet_id"])
    with pytest.raises(PermissionDenied):
        await service.unlink(gid, 44, husband["id"])
    with pytest.raises(PermissionDenied):
        await service.set_settler(gid, 44, husband["id"])
    with pytest.raises(ValueError, match="active members"):
        await service.link_wallets(gid, 11, sister["wallet_id"], dan["wallet_id"])
    with pytest.raises(ValueError):
        await service.unlink(gid, 11, dan["id"])
    with pytest.raises(ValueError):
        await service.set_settler(gid, 11, dan["id"])
    await service.add_expense(gid, 11, {"amount_minor": 300, "description": "Bill"})
    before = await service.snapshot(gid)
    with pytest.raises(ValueError, match="zero balance"):
        await service.link_wallets(gid, 11, sister["wallet_id"], admin["wallet_id"])
    with pytest.raises(ValueError, match="zero balance"):
        await service.unlink(gid, 11, husband["id"])
    assert await service.snapshot(gid) == before
    # Changing who settles does not move debt and is allowed with an outstanding balance.
    await service.set_settler(gid, 11, husband["id"])
    assert (await service.snapshot(gid))["balances"] == before["balances"]


async def test_member_link_preserves_history_and_departed_members(service, household):
    group, (admin, sister, husband, dan) = household
    gid = group["id"]
    await service.link_wallets(gid, 11, dan["wallet_id"], husband["wallet_id"])
    expense = await service.add_expense(gid, 33, {"amount_minor": 400, "description": "Before setup"})
    await service.add_transfer(
        gid,
        11,
        {"amount_minor": 100, "from_wallet_id": admin["wallet_id"], "to_wallet_id": husband["wallet_id"]},
    )
    await service.add_transfer(
        gid,
        22,
        {"amount_minor": 100, "from_wallet_id": sister["wallet_id"], "to_wallet_id": husband["wallet_id"]},
    )
    await service.leave(gid, 44)
    before = await service.snapshot(gid)
    await service.link_wallets(gid, 11, husband["wallet_id"], sister["wallet_id"])
    after = await service.snapshot(gid)
    assert after["expenses"] == before["expenses"]
    assert after["transfers"] == before["transfers"]
    assert next(m for m in after["members"] if m["id"] == dan["id"])["wallet_id"] == husband["wallet_id"]
    assert not next(w for w in after["wallets"] if w["id"] == husband["wallet_id"])["closed_at"]
    assert next(m for m in after["members"] if m["id"] == husband["id"])["wallet_id"] == sister["wallet_id"]
    assert after["expenses"][0]["payer_wallet_id"] == expense["payer_wallet_id"]


async def test_member_link_closed_history_stays_controlled_by_members(service, household):
    group, (admin, sister, husband, dan) = household
    gid = group["id"]
    expense = await service.add_expense(
        gid, 33, {"amount_minor": 200, "member_ids": [sister["id"], husband["id"]]}
    )
    await service.add_transfer(
        gid,
        22,
        {"amount_minor": 100, "from_wallet_id": sister["wallet_id"], "to_wallet_id": husband["wallet_id"]},
    )
    await service.link_wallets(gid, 11, husband["wallet_id"], sister["wallet_id"])
    state = await service.snapshot(gid, 22)
    assert husband["wallet_id"] in state["me"]["wallet_ids"]
    edited = await service.edit_expense(gid, 22, expense["id"], {"amount_minor": 400})
    assert edited["payer_wallet_id"] == husband["wallet_id"]
    assert [s["wallet_id"] for s in edited["shares"]] == [s["wallet_id"] for s in expense["shares"]]
    await service.add_transfer(
        gid,
        22,
        {"amount_minor": 100, "from_wallet_id": sister["wallet_id"], "to_wallet_id": husband["wallet_id"]},
    )
    assert all(value == 0 for value in (await service.snapshot(gid))["balances"].values())


@pytest.mark.parametrize("invalid_id", ["1", True, None, [], {}, 0, -1, 1.0])
async def test_member_link_rejects_invalid_wallet_ids(service, household, invalid_id):
    group, (admin, sister, husband, dan) = household
    with pytest.raises(ValueError, match="valid wallet IDs"):
        await service.link_wallets(group["id"], 11, invalid_id, sister["wallet_id"])
    with pytest.raises(ValueError, match="valid wallet IDs"):
        await service.link_wallets(group["id"], 11, sister["wallet_id"], invalid_id)


async def test_add_people_directly_to_wallets_without_rewriting_history(service, household):
    group, (a, sister, husband, dan) = household
    gid = group["id"]
    entry = await service.add_expense(gid, 11, {"amount_minor": 40000, "description": "Before onboarding"})
    before = await service.snapshot(gid)
    child = await service.add_guest(gid, 44, "Child", sister["wallet_id"])
    state = await service.snapshot(gid)
    assert child["wallet_id"] == sister["wallet_id"]
    assert len(state["wallets"]) == len(before["wallets"])
    assert state["expenses"] == before["expenses"]
    assert state["balances"] == before["balances"]
    assert (
        next(w for w in state["wallets"] if w["id"] == sister["wallet_id"])["settler_member_id"]
        == sister["id"]
    )
    own = await service.add_guest(gid, 44, "Parent")
    assert own["wallet_id"] != sister["wallet_id"]
    after = await service.add_expense(gid, 11, {"amount_minor": 600, "description": "After onboarding"})
    assert (
        next(s for s in after["shares"] if s["member_id"] == child["id"])["wallet_id"] == sister["wallet_id"]
    )
    assert child["id"] not in {s["member_id"] for s in entry["shares"]}


async def test_assign_individual_members_and_keep_other_wallet_members_in_place(service, household):
    group, (a, sister, husband, dan) = household
    gid = group["id"]
    await service.link_wallets(gid, 44, husband["wallet_id"], sister["wallet_id"])
    invite = await service.invite(gid, 22, dan["id"])
    moved = await service.assign_wallet(gid, 11, sister["id"], a["wallet_id"])
    state = await service.snapshot(gid)
    assert moved["wallet_id"] == a["wallet_id"]
    assert next(m for m in state["members"] if m["id"] == husband["id"])["wallet_id"] == sister["wallet_id"]
    assert (
        next(w for w in state["wallets"] if w["id"] == sister["wallet_id"])["settler_member_id"]
        == husband["id"]
    )
    assert next(w for w in state["wallets"] if w["id"] == a["wallet_id"])["settler_member_id"] == a["id"]
    with pytest.raises(ValueError, match="no longer pending"):
        await service.respond_invite(gid, 44, invite["id"], True)
    # Assigning someone to their current wallet does not change balances or create a wallet.
    await service.assign_wallet(gid, 44, sister["id"], a["wallet_id"])
    assert await service.snapshot(gid) == state
    separate = await service.assign_wallet(gid, 44, sister["id"])
    assert separate["wallet_id"] not in {a["wallet_id"], sister["wallet_id"]}
    async with service.db.transaction():
        audit = await service.db.one("SELECT * FROM audit_log WHERE action='assign' ORDER BY id DESC LIMIT 1")
    assert audit["member_id"] == a["id"] and audit["entity_id"] == sister["id"]


async def test_member_creation_and_assignment_validate_membership_groups_and_balances(service, household):
    group, (a, sister, husband, dan) = household
    gid = group["id"]
    other = await service.ensure_group(-99887, "Other")
    outsider = await service.join(other["id"], 55, "Outsider")
    before = await service.snapshot(gid)
    with pytest.raises(PermissionDenied):
        await service.add_guest(gid, 55, "Nope", sister["wallet_id"])
    with pytest.raises(PermissionDenied):
        await service.assign_wallet(gid, 55, husband["id"], sister["wallet_id"])
    for wallet_id in (outsider["wallet_id"], True, "1", 0):
        with pytest.raises(ValueError):
            await service.add_guest(gid, 44, "Nope", wallet_id)
        with pytest.raises(ValueError):
            await service.assign_wallet(gid, 44, husband["id"], wallet_id)
    with pytest.raises(ValueError):
        await service.assign_wallet(gid, 44, outsider["id"], sister["wallet_id"])
    with pytest.raises(ValueError):
        await service.add_guest(gid, 44, "", sister["wallet_id"])
    assert await service.snapshot(gid) == before
    await service.add_expense(gid, 11, {"amount_minor": 200, "member_ids": [a["id"], sister["id"]]})
    before = await service.snapshot(gid)
    with pytest.raises(ValueError, match="zero balance"):
        await service.assign_wallet(gid, 44, husband["id"], sister["wallet_id"])
    with pytest.raises(ValueError, match="zero balance"):
        await service.assign_wallet(gid, 44, sister["id"], husband["wallet_id"])
    assert await service.snapshot(gid) == before
    await service.leave(gid, 33)
    with pytest.raises(ValueError, match="active members"):
        await service.add_guest(gid, 44, "Nope", husband["wallet_id"])
    with pytest.raises(ValueError):
        await service.assign_wallet(gid, 44, husband["id"], dan["wallet_id"])


async def test_joined_members_can_administer_but_outsiders_and_former_members_cannot(service, household):
    group, (a, sister, husband, dan) = household
    gid = group["id"]
    await service.update_settings(
        gid, 44, {"currency": "USD", "minor_units": 0, "weights": {str(sister["id"]): "0.5"}}
    )
    await service.link_wallets(gid, 44, husband["wallet_id"], sister["wallet_id"])
    entry = await service.add_expense(gid, 11, {"amount_minor": 700, "description": "Shared debt"})
    before = await service.snapshot(gid)
    await service.leave(gid, 44, sister["id"])
    after = await service.snapshot(gid)
    assert after["expenses"] == before["expenses"] and after["balances"] == before["balances"]
    assert (
        next(w for w in after["wallets"] if w["id"] == sister["wallet_id"])["settler_member_id"]
        == husband["id"]
    )
    assert after["expenses"][0]["id"] == entry["id"]
    other = await service.ensure_group(-88776, "Other")
    outsider = await service.join(other["id"], 55, "Outsider")
    for uid in (999, 55, 22):
        with pytest.raises(PermissionDenied):
            await service.update_settings(gid, uid, {"split_mode": "wallet"})
        with pytest.raises(PermissionDenied):
            await service.leave(gid, uid, husband["id"])
        with pytest.raises(PermissionDenied):
            await service.forget(gid, uid, confirmation=group["token"])
    with pytest.raises(ValueError):
        await service.leave(gid, 44, outsider["id"])
    with pytest.raises(PermissionDenied):
        await service.forget(gid, 44, confirmation="wrong")
    await service.forget(gid, 44, confirmation=group["token"])
    assert await service.group_for_token(group["token"]) is None


async def test_assigning_a_settled_wallet_keeps_its_history_and_control(service, household):
    group, (a, sister, husband, dan) = household
    gid = group["id"]
    expense = await service.add_expense(
        gid,
        33,
        {
            "amount_minor": 2000,
            "description": "Historical dinner",
            "member_ids": [sister["id"], husband["id"]],
        },
    )
    await service.add_transfer(
        gid,
        22,
        {"amount_minor": 1000, "from_wallet_id": sister["wallet_id"], "to_wallet_id": husband["wallet_id"]},
    )
    before = await service.snapshot(gid)
    await service.assign_wallet(gid, 44, husband["id"], sister["wallet_id"])
    after = await service.snapshot(gid, 22)
    assert after["expenses"] == [expense | {"can_edit": True, "can_review": True}]
    assert after["transfers"][0]["from_wallet_id"] == before["transfers"][0]["from_wallet_id"]
    assert next(w for w in after["wallets"] if w["id"] == husband["wallet_id"])["closed_at"]
    assert husband["wallet_id"] in after["me"]["wallet_ids"]
    edited = await service.edit_expense(gid, 22, expense["id"], {"amount_minor": 4000})
    assert edited["payer_wallet_id"] == husband["wallet_id"]
    assert [s["wallet_id"] for s in edited["shares"]] == [s["wallet_id"] for s in expense["shares"]]
