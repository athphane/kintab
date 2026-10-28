import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from pyrogram import enums

from bot.config import Settings
from bot.webapp import create_app, verify_init_data

TOKEN = "test-token-not-a-real-secret"


def signed(user_id=11, **fields):
    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id}), **fields}
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class FakeTelegram:
    def __init__(self):
        self.admins = {11}
        self.removed = set()

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(
            status=enums.ChatMemberStatus.LEFT
            if user_id in self.removed
            else enums.ChatMemberStatus.ADMINISTRATOR
            if user_id in self.admins
            else enums.ChatMemberStatus.MEMBER
        )


class QuietNotifier:
    def __init__(self):
        self.events = []

    async def changed(self, *args, **kwargs):
        self.events.append((args, kwargs))

    async def review(self, *args):
        self.events.append(args)

    async def invite(self, *args):
        self.events.append(args)

    def schedule(self, group_id):
        pass


@pytest.fixture
async def web_context(service, household, aiohttp_client):
    settings = Settings(api_id=1, api_hash="test", bot_token=TOKEN, bot_username="KinTabTestBot")
    telegram, notifier = FakeTelegram(), QuietNotifier()
    client = await aiohttp_client(create_app(settings, service, telegram, notifier))
    group, people = household
    return client, group, people, telegram, notifier


def test_init_data_tampering_expiry_duplicate_fields_and_bad_user():
    assert verify_init_data(signed(), TOKEN)["user"]["id"] == 11
    assert verify_init_data(signed(), "wrong-token") is None
    assert verify_init_data(signed(auth_date="1"), TOKEN) is None
    assert verify_init_data(signed(auth_date=str(int(time.time()) + 100)), TOKEN) is None
    assert verify_init_data(signed() + "&auth_date=1", TOKEN) is None
    assert verify_init_data(signed(user="[]"), TOKEN) is None
    assert verify_init_data(signed(user='{"id":true}'), TOKEN) is None
    assert verify_init_data("", TOKEN) is None


async def test_public_page_exposes_only_configured_bot_link(web_context):
    client, _, _, _, _ = web_context
    response = await client.get("/")
    html = await response.text()
    assert response.status == 200
    assert '<meta name="kintab-bot-url" content="https://t.me/KinTabTestBot">' in html
    assert "__KINTAB_BOT_URL__" not in html
    assert TOKEN not in html


async def test_all_routes_require_signed_data_and_group_membership(web_context):
    client, g, people, telegram, _ = web_context
    base = f"/api/{g['token']}/"
    for route in ["state", "history", "summary", "breakdown", "export"]:
        assert (await client.get(base + route)).status == 401
        assert (await client.get(base + route, headers={"X-Telegram-Init-Data": signed(999)})).status == 403
    assert (await client.post(base + "action", json={"action": "guest", "name": "Injected"})).status == 401
    assert (
        await client.get(base + "state", headers={"X-Telegram-Init-Data": signed(start_param="wrong")})
    ).status == 403
    headers = {"X-Telegram-Init-Data": signed(start_param=g["token"])}
    assert (await client.get(base + "state", headers=headers)).status == 200
    telegram.removed.add(11)
    assert (await client.get(base + "state", headers=headers)).status == 403
    assert (
        await client.post(base + "action", headers=headers, json={"action": "guest", "name": "Nope"})
    ).status == 403


async def test_api_crud_breakdown_summary_filters_and_permission_parity(web_context, service):
    client, g, (a, s, h, d), telegram, notifier = web_context
    base = f"/api/{g['token']}/"
    headers = {"X-Telegram-Init-Data": signed()}

    async def action(data, uid=11, status=200):
        response = await client.post(
            base + "action", headers={"X-Telegram-Init-Data": signed(uid)}, json=data
        )
        payload = await response.json()
        assert response.status == status, payload
        return payload

    result = await action(
        {"action": "add_expense", "amount_minor": 10001, "description": "Bill", "category": "Utilities"}
    )
    entry = result["entry"]
    assert len(notifier.events) == 1
    await action({"action": "delete_expense", "id": entry["id"]}, uid=22, status=403)
    await action({"action": "review_expense", "id": entry["id"]}, uid=22)
    await action({"action": "edit_expense", "id": entry["id"], "amount_minor": 12000, "revision": 1})
    await action(
        {"action": "edit_expense", "id": entry["id"], "amount_minor": 13000, "revision": 1}, status=400
    )
    history = await (
        await client.get(base + "history?category=Utilities&payer=" + str(a["id"]), headers=headers)
    ).json()
    assert len(history["expenses"]) == 1 and history["expenses"][0]["amount_minor"] == 12000
    assert not (await (await client.get(base + "history?category=Groceries", headers=headers)).json())[
        "expenses"
    ]
    breakdown = await (await client.get(base + "breakdown", headers=headers)).json()
    assert breakdown["balance_minor"] == 9000
    assert sum(row["delta_minor"] for row in breakdown["rows"]) == 9000
    summary = await (
        await client.get(base + "summary?month=" + entry["created_at"][:7], headers=headers)
    ).json()
    assert summary["total_minor"] == 12000
    assert sum(summary["wallet_shares"].values()) == 12000
    await action({"action": "settings", "currency": "USD"}, uid=22, status=400)
    # Group settings are available to ordinary members as well.
    telegram.admins.clear()
    await action({"action": "settings", "split_mode": "wallet"}, uid=22)
    telegram.admins.add(11)
    await action({"action": "settings", "split_mode": "wallet"})
    await action(
        {
            "action": "add_transfer",
            "amount_minor": 3000,
            "from_wallet_id": s["wallet_id"],
            "to_wallet_id": a["wallet_id"],
        },
        uid=22,
    )
    export = await client.get(base + "export", headers=headers)
    assert export.status == 200 and "audit" in await export.text()
    await action({"action": "delete_expense", "id": entry["id"]})
    assert (await (await client.get(base + "breakdown", headers=headers)).json())["balance_minor"] == -3000


async def test_pages_assets_json_errors_and_group_deletion(web_context):
    client, g, people, telegram, notifier = web_context
    index = await client.get("/")
    html = await index.text()
    assert index.status == 200 and "/static/app.js?v=" in html
    assert (await client.get("/static/app.js")).status == 200
    assert (await client.get("/healthz")).status == 200
    path = f"/api/{g['token']}/action"
    headers = {"X-Telegram-Init-Data": signed()}
    assert (await client.post(path, headers=headers, data="{broken")).status == 400
    assert (
        await client.post(path, headers=headers, json={"action": "forget", "confirmation": "no"})
    ).status == 403
    assert (
        await client.post(path, headers=headers, json={"action": "forget", "confirmation": g["token"]})
    ).status == 200
    assert (await client.get(f"/api/{g['token']}/state", headers=headers)).status == 404


async def test_wallet_setup_is_available_to_members_but_still_requires_membership(web_context, service):
    client, group, (admin, ibrahim, fathimath, hassan), telegram, notifier = web_context
    path = f"/api/{group['token']}/action"

    async def action(data, uid=44, status=200):
        response = await client.post(path, headers={"X-Telegram-Init-Data": signed(uid)}, json=data)
        assert response.status == status, await response.json()

    actions = [
        {
            "action": "link_wallets",
            "from_wallet_id": fathimath["wallet_id"],
            "to_wallet_id": ibrahim["wallet_id"],
        },
        {"action": "settler", "member_id": fathimath["id"]},
        {"action": "unlink", "member_id": fathimath["id"]},
    ]
    for data in actions:
        await action(data, uid=999, status=403)
    telegram.removed.add(44)
    for data in actions:
        await action(data, status=403)
    telegram.removed.remove(44)
    for data in actions:
        await action(data)
    state = await service.snapshot(group["id"])
    assert (
        next(m for m in state["members"] if m["id"] == fathimath["id"])["wallet_id"] != ibrahim["wallet_id"]
    )
    # Losing admin status does not remove the ability to help with wallet setup.
    telegram.admins.clear()
    await action({"action": "settler", "member_id": ibrahim["id"]}, uid=11)
    await action({"action": "settings", "split_mode": "wallet"})
    # Invitations and the existing self-service unlink action still work.
    await action({"action": "invite", "member_id": hassan["id"]}, uid=22)
    async with service.db.transaction():
        invitation = await service.db.one("SELECT id FROM wallet_invites WHERE status='pending'")
    await action({"action": "respond_invite", "id": invitation["id"], "accept": True})
    await action({"action": "unlink"})
    await service.leave(group["id"], 44)
    for data in actions:
        await action(data, status=403)


@pytest.mark.parametrize("kind", ["settings", "leave", "forget", "member", "assign_wallet"])
async def test_member_administration_api_checks_membership_on_every_action(web_context, service, kind):
    client, group, (a, ibrahim, fathimath, hassan), telegram, _ = web_context
    path = f"/api/{group['token']}/action"
    data = {
        "settings": {"action": "settings", "currency": "USD", "minor_units": 0},
        "leave": {"action": "leave", "member_id": ibrahim["id"]},
        "forget": {"action": "forget", "confirmation": group["token"]},
        "member": {"action": "member", "name": "Mariyam", "wallet_id": ibrahim["wallet_id"]},
        "assign_wallet": {
            "action": "assign_wallet",
            "member_id": fathimath["id"],
            "wallet_id": ibrahim["wallet_id"],
        },
    }[kind]

    async def action(uid=44):
        return await client.post(path, headers={"X-Telegram-Init-Data": signed(uid)}, json=data)

    assert (await client.post(path, json=data)).status == 401
    assert (await action(999)).status == 403
    telegram.removed.add(44)
    assert (await action()).status == 403
    telegram.removed.remove(44)
    await service.leave(group["id"], 44)
    assert (await action()).status == 403
    await service.join(group["id"], 44, "Hassan")
    response = await action()
    assert response.status == 200, await response.json()
    if kind == "forget":
        assert await service.group_for_token(group["token"]) is None
    else:
        state = await service.snapshot(group["id"])
        if kind == "settings":
            assert state["group"]["currency"] == "USD" and state["group"]["minor_units"] == 0
        elif kind == "leave":
            assert next(m for m in state["members"] if m["id"] == ibrahim["id"])["left_at"]
        else:
            person = next(
                m for m in state["members"] if m["name"] == ("Mariyam" if kind == "member" else "Fathimath")
            )
            assert person["wallet_id"] == ibrahim["wallet_id"]
