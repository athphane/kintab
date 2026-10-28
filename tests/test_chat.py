from types import SimpleNamespace

import pytest
from pyrogram import enums
from pyrogram.errors import ChatAdminRequired

from bot.config import Settings
from bot.handlers import ChatHandlers
from bot.notify import Notifier


class Message:
    def __init__(self, client, chat_id, text="", user_id=11, message_id=1, reply=None):
        self.client = client
        self.chat = SimpleNamespace(id=chat_id, title="Household", type=enums.ChatType.SUPERGROUP)
        self.from_user = SimpleNamespace(
            id=user_id, first_name=f"User {user_id}", username=None, is_bot=False
        )
        self.command = [text.split()[0].removeprefix("/")] if text else []
        self.id, self.text, self.reply_to_message = message_id, text, reply
        self.migrate_to_chat_id = self.migrate_from_chat_id = self.left_chat_member = None
        self.new_chat_members = []

    async def reply_text(self, text, **kwargs):
        return await self.client.send_message(self.chat.id, text, **kwargs)

    async def edit_text(self, text, **kwargs):
        return await self.client.edit_message_text(self.chat.id, self.id, text, **kwargs)

    async def edit_reply_markup(self, **kwargs):
        pass


class FakeClient:
    def __init__(self):
        self.messages, self.edits, self.photos = [], [], []
        self.me = SimpleNamespace(id=1000)
        self.pin_allowed = False

    async def send_message(self, chat_id, text, **kwargs):
        message = Message(self, chat_id, text, message_id=100 + len(self.messages))
        message.reply_markup = kwargs.get("reply_markup")
        self.messages.append(message)
        return message

    async def send_photo(self, chat_id, photo, **kwargs):
        self.photos.append(SimpleNamespace(chat_id=chat_id, image=photo.getvalue(), **kwargs))

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        self.edits.append((chat_id, message_id, text))

    async def pin_chat_message(self, *args, **kwargs):
        if not self.pin_allowed:
            raise ChatAdminRequired()

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status=enums.ChatMemberStatus.MEMBER)


class Query:
    def __init__(self, client, chat_id, user_id, data, message_id=999):
        self.message = Message(client, chat_id, message_id=message_id)
        self.from_user = self.message.from_user = SimpleNamespace(
            id=user_id, first_name=f"User {user_id}", username=None
        )
        self.data = data
        self.answers = []

    async def answer(self, text="", **kwargs):
        self.answers.append((text, kwargs))


@pytest.fixture
async def chat(service):
    client = FakeClient()
    settings = Settings(api_id=1, api_hash="test", bot_token="test", bot_username="KinTabTestBot")
    notifier = Notifier(client, service, settings)
    yield client, notifier, ChatHandlers(client, service, notifier)
    await notifier.close()


async def test_startgroup_link_posts_household_welcome(service, chat):
    client, _, handlers = chat
    await handlers.command(client, Message(client, -987, "/start@KinTabTestBot welcome"))
    group = await service.ensure_group(-987, "Household")
    state = await service.snapshot(group["id"])
    assert state["members"] == [] and state["expenses"] == []
    assert any("I'm in" in message.text for message in client.messages)


async def test_setup_join_commands_payer_and_unauthorized_buttons(service, chat):
    client, notifier, handlers = chat
    await handlers.command(client, Message(client, -123, "/start"))
    group = await service.ensure_group(-123, "Household")
    for uid in [11, 22, 33]:
        await handlers.callback(client, Query(client, -123, uid, f"j:{group['id']}"))
    await handlers.command(client, Message(client, -123, "/spent 900 groceries", message_id=10))
    state = await service.snapshot(group["id"])
    assert len(state["expenses"]) == 1 and state["expenses"][0]["amount_minor"] == 90000
    entry = state["expenses"][0]
    assert "Running total:" in client.messages[-1].text
    assert any(
        b.text == "Open tab" and b.url
        for row in client.messages[-1].reply_markup.inline_keyboard
        for b in row
    )
    query = Query(client, -123, 22, f"d:{entry['id']}")
    await handlers.callback(client, query)
    assert query.answers[-1][1]["show_alert"]
    assert not (await service.snapshot(group["id"]))["expenses"][0]["deleted_at"]
    # Reply-based payer resolution works without a Telegram username.
    await handlers.command(
        client,
        Message(
            client, -123, "/spent 1200 electricity", message_id=11, reply=Message(client, -123, user_id=22)
        ),
    )
    assert (await service.snapshot(group["id"]))["expenses"][1]["payer_member_id"] == state["members"][1][
        "id"
    ]
    await handlers.command(client, Message(client, -123, "/spent 100 outsider", user_id=99))
    assert len((await service.snapshot(group["id"]))["expenses"]) == 2
    assert "join this tab first" in client.messages[-1].text


async def test_tab_command_replies_with_button_when_total_already_exists(chat, household):
    client, notifier, handlers = chat
    group, _ = household
    await notifier.pin(group["id"])
    count = len(client.messages)
    await handlers.command(client, Message(client, group["chat_id"], "/tab"))
    assert len(client.messages) == count + 1
    reply = client.messages[-1]
    assert "KinTab" in reply.text and "All settled" in reply.text
    assert reply.reply_markup.inline_keyboard[0][0].text == "Open tab"
    assert reply.reply_markup.inline_keyboard[0][0].url.startswith("https://t.me/KinTabTestBot/app?startapp=")


async def test_pin_denied_warns_once_and_recovers(chat, household):
    client, notifier, handlers = chat
    group, people = household
    await notifier.pin(group["id"])
    await notifier.pin(group["id"])
    assert len([m for m in client.messages if "Give me admin" in m.text]) == 1
    client.pin_allowed = True
    await notifier.pin(group["id"])
    state = await notifier.service.snapshot(group["id"])
    assert state["group"]["pin_ok"] == 1
    keys = notifier.tab_keyboard(state["group"])
    assert keys.inline_keyboard[0][0].url.startswith("https://t.me/KinTabTestBot/app?startapp=")
    for row in keys.inline_keyboard:
        for b in row:
            if b.callback_data:
                assert len(b.callback_data.encode()) <= 64


async def test_edit_reply_and_payment_recipient_buttons(chat, household):
    client, notifier, handlers = chat
    service = notifier.service
    g, people = household
    entry = await service.add_expense(g["id"], 11, {"amount_minor": 1000, "description": "Groceries"})
    await handlers.callback(client, Query(client, g["chat_id"], 11, f"e:{entry['id']}"))
    prompt = client.messages[-1]
    await handlers.reply(client, Message(client, g["chat_id"], "1200 Groceries", reply=prompt, message_id=22))
    assert (await service.snapshot(g["id"]))["expenses"][0]["amount_minor"] == 120000
    assert any("✏️" in m.text for m in client.messages)
    # Multiple creditors: the sender must choose; another person's tap must fail.
    await handlers.command(client, Message(client, g["chat_id"], "/paid 300", user_id=11, message_id=23))
    choice = client.messages[-1].reply_markup.inline_keyboard[0][0].callback_data
    forbidden = Query(client, g["chat_id"], 22, choice)
    await handlers.callback(client, forbidden)
    assert forbidden.answers[-1][1]["show_alert"]
    await handlers.callback(client, Query(client, g["chat_id"], 11, choice))
    assert len((await service.snapshot(g["id"]))["transfers"]) == 1


async def test_refresh_during_pin_is_not_lost(chat, household, monkeypatch):
    import asyncio

    client, notifier, _ = chat
    group, _ = household
    calls = []

    async def pin(group_id):
        calls.append(group_id)
        if len(calls) == 1:
            notifier.schedule(group_id)

    original_sleep = asyncio.sleep

    async def fast_sleep(seconds):
        await original_sleep(0)

    monkeypatch.setattr(notifier, "pin", pin)
    monkeypatch.setattr("bot.notify.asyncio.sleep", fast_sleep)
    notifier.schedule(group["id"])
    await notifier.tasks[group["id"]]
    assert calls == [group["id"], group["id"]]


async def test_concurrent_entry_delivery_uses_one_message_and_latest_content(chat, household, monkeypatch):
    import asyncio

    client, notifier, _ = chat
    group, _ = household
    entry = await notifier.service.add_expense(group["id"], 11, {"amount_minor": 1000})
    sending, release = asyncio.Event(), asyncio.Event()
    original_send = client.send_message

    async def delayed_send(chat_id, content, **kwargs):
        if content.startswith("🛒"):
            sending.set()
            await release.wait()
        return await original_send(chat_id, content, **kwargs)

    monkeypatch.setattr(client, "send_message", delayed_send)
    creating = asyncio.create_task(notifier.changed(group["id"], "expense", entry))
    await sending.wait()
    edited = await notifier.service.edit_expense(group["id"], 11, entry["id"], {"amount_minor": 2000})
    updating = asyncio.create_task(
        notifier.changed(group["id"], "expense", edited, action="edit", before=entry, actor_id=11)
    )
    release.set()
    await asyncio.gather(creating, updating)
    assert len([m for m in client.messages if m.text.startswith("🛒")]) == 1
    assert "MVR 20.00" in client.edits[-1][2]


@pytest.mark.parametrize("command", ["/damage", "/owed", "/damage@KinTabTestBot"])
async def test_damage_command_posts_a_photo_without_changing_the_tab(chat, household, command):
    import io

    from PIL import Image

    client, notifier, handlers = chat
    group, _ = household
    await notifier.service.add_expense(group["id"], 11, {"amount_minor": 40000, "description": "Dinner"})
    before = await notifier.service.snapshot(group["id"])
    await handlers.command(client, Message(client, group["chat_id"], command))
    assert await notifier.service.snapshot(group["id"]) == before
    assert not client.messages and not client.edits and not notifier.tasks
    photo = client.photos[0]
    assert photo.chat_id == group["chat_id"]
    assert "who owes whom" in photo.caption and "Simplified" in photo.caption
    assert photo.reply_markup.inline_keyboard[0][0].text == "Open tab"
    with Image.open(io.BytesIO(photo.image)) as image:
        assert image.format == "PNG"
        image.verify()


async def test_damage_command_requires_a_group(chat):
    client, _, handlers = chat
    message = Message(client, 11, "/damage")
    message.chat.type = enums.ChatType.PRIVATE
    await handlers.command(client, message)
    assert not client.photos
    assert "Use this command in your household group" in client.messages[-1].text


async def test_any_joined_member_can_delete_in_chat_with_caller_bound_confirmation(chat, household):
    client, notifier, handlers = chat
    group, _ = household
    service = notifier.service
    await handlers.command(client, Message(client, group["chat_id"], "/forget", user_id=999))
    assert "join this tab first" in client.messages[-1].text
    await handlers.command(client, Message(client, group["chat_id"], "/forget", user_id=44))
    prompt = client.messages[-1]
    confirm = prompt.reply_markup.inline_keyboard[0][0].callback_data
    cancel = prompt.reply_markup.inline_keyboard[1][0].callback_data
    other_user = Query(client, group["chat_id"], 11, confirm)
    await handlers.callback(client, other_user)
    assert other_user.answers[-1][1]["show_alert"]
    assert await service.group_for_token(group["token"])
    await handlers.callback(client, Query(client, group["chat_id"], 44, cancel))
    assert await service.group_for_token(group["token"])
    await handlers.command(client, Message(client, group["chat_id"], "/forget", user_id=44))
    confirm = client.messages[-1].reply_markup.inline_keyboard[0][0].callback_data
    await service.leave(group["id"], 44)
    former_member = Query(client, group["chat_id"], 44, confirm)
    await handlers.callback(client, former_member)
    assert former_member.answers[-1][1]["show_alert"]
    assert await service.group_for_token(group["token"])
    await service.join(group["id"], 44, "Hassan")
    await handlers.callback(client, Query(client, group["chat_id"], 44, confirm))
    assert await service.group_for_token(group["token"]) is None


async def test_chat_deletion_rechecks_telegram_membership(chat, household, monkeypatch):
    client, notifier, handlers = chat
    group, _ = household
    await handlers.command(client, Message(client, group["chat_id"], "/forget", user_id=44))
    confirm = client.messages[-1].reply_markup.inline_keyboard[0][0].callback_data

    async def removed(chat_id, user_id):
        return SimpleNamespace(status=enums.ChatMemberStatus.LEFT)

    monkeypatch.setattr(client, "get_chat_member", removed)
    query = Query(client, group["chat_id"], 44, confirm)
    await handlers.callback(client, query)
    assert query.answers[-1][1]["show_alert"]
    assert await notifier.service.group_for_token(group["token"])
