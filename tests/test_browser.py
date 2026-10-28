"""Run explicitly: KINTAB_BROWSER_TEST=1 uv run pytest tests/test_browser.py."""

import json
import os
from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect

from bot.config import Settings
from bot.webapp import create_app
from tests.test_webapp import TOKEN, FakeTelegram, QuietNotifier, signed

pytestmark = pytest.mark.skipif(os.environ.get("KINTAB_BROWSER_TEST") != "1", reason="Explicit browser test")


async def test_mobile_workflow_real_api(service, household, aiohttp_server, tmp_path):
    group, people = household
    app = create_app(Settings(1, "test", TOKEN, "KinTabTestBot"), service, FakeTelegram(), QuietNotifier())
    server = await aiohttp_server(app)
    async with async_playwright() as playwright:
        executable = os.environ.get("KINTAB_CHROMIUM")
        browser = await playwright.chromium.launch(executable_path=executable)
        context = await browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=1)
        await context.route("https://telegram.org/**", lambda route: route.abort())
        await context.route("https://fonts.googleapis.com/**", lambda route: route.abort())
        await context.add_init_script(
            "window.Telegram={WebApp:{ready(){},expand(){},HapticFeedback:{notificationOccurred(){},selectionChanged(){}}}}"
        )
        await context.add_init_script("window.addEventListener('DOMContentLoaded',()=>{},false)")
        # The signed data comes from the test Telegram authority, never a production bot.
        import json

        await context.add_init_script(
            f"window.Telegram.WebApp.initData={json.dumps(signed(start_param=group['token']))};"
            f"window.Telegram.WebApp.initDataUnsafe={{start_param:{json.dumps(group['token'])}}};"
        )
        page = await context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(str(server.make_url("/")))
        await page.get_by_role("heading", name="A little more in balance.").wait_for()
        await page.get_by_role("link", name="＋ Add expense", exact=True).first.click()
        await page.get_by_label("Amount · MVR").fill("900")
        await page.get_by_label("Description", exact=True).fill("Groceries")
        await page.get_by_role("button", name="Add expense", exact=True).click()
        await page.get_by_role("heading", name="The whole picture.").wait_for()
        await page.get_by_text("Groceries", exact=True).wait_for()
        assert (await service.snapshot(group["id"]))["expenses"][0]["amount_minor"] == 90000
        await page.get_by_role("link", name="Edit", exact=True).click()
        await page.get_by_label("Amount · MVR").fill("1200")
        await page.get_by_role("button", name="Save changes", exact=True).click()
        await page.get_by_role("heading", name="The whole picture.").wait_for()
        assert (await service.snapshot(group["id"]))["expenses"][0]["amount_minor"] == 120000
        await page.get_by_role("link", name="Monthly", exact=False).click()
        await page.get_by_role("heading", name="A month, accounted for.").wait_for()
        assert "1,200.00" in await page.locator(".balance-card .amount").inner_text()
        await page.get_by_role("link", name="Group", exact=False).click()
        await page.get_by_role("heading", name="A household, together.").wait_for()
        await page.get_by_label("Member name", exact=True).fill("Parent")
        await page.get_by_role("button", name="Add member", exact=True).click()
        await page.get_by_role("heading", name="A household, together.").wait_for()
        await page.get_by_role("heading", name="Parent", exact=True).wait_for()
        await page.goto(str(server.make_url("/")) + "#expense")
        await page.get_by_label("Amount · MVR").fill("100")
        await page.get_by_label("Description", exact=True).fill('<img src=x onerror="window.XSS=true">')
        await page.get_by_label("Split between").select_option("custom")
        await page.locator('input[name="participant"]').nth(2).uncheck()
        await page.locator('input[name="participant"]').nth(3).uncheck()
        await page.locator('input[name="participant"]').nth(4).uncheck()
        await page.get_by_label("Weight for Sister").fill("0.5")
        await page.get_by_role("button", name="Add expense", exact=True).click()
        await page.get_by_role("heading", name="The whole picture.").wait_for()
        assert not await page.evaluate("Boolean(window.XSS)")
        state = await service.snapshot(group["id"])
        assert [s["amount_minor"] for s in state["expenses"][1]["shares"]] == [6667, 3333]
        await page.locator("article.entry").filter(has_text="<img src=x").get_by_role("link", name="Edit").click()
        await expect(page.get_by_text("Current shares")).to_be_visible()
        await expect(page.get_by_label("Change who shares this expense")).to_be_checked()
        await expect(page.get_by_label("Split between")).to_be_visible()
        await expect(page.get_by_label("Split between")).to_have_value("custom")
        await expect(page.get_by_label("Weight for Sister")).to_have_value("0.5")
        participants = page.locator('input[name="participant"]')
        assert [await participants.nth(i).is_checked() for i in range(5)] == [True, True, False, False, False]
        await page.get_by_label("Amount · MVR").fill("120")
        await page.get_by_role("button", name="Save changes", exact=True).click()
        await page.get_by_role("heading", name="The whole picture.").wait_for()
        state = await service.snapshot(group["id"])
        assert [s["amount_minor"] for s in state["expenses"][1]["shares"]] == [8000, 4000]
        await page.goto(str(server.make_url("/")) + "#breakdown")
        await page.get_by_role("heading", name="Every part of the balance.").wait_for()
        await page.goto(str(server.make_url("/")) + "#settle")
        await page.get_by_role("heading", name="Bring things even.").wait_for()
        await page.get_by_role("link", name="Record payment", exact=True).first.click()
        await page.get_by_role("button", name="Save payment", exact=True).click()
        await page.get_by_role("heading", name="Bring things even.").wait_for()
        assert len((await service.snapshot(group["id"]))["transfers"]) == 1
        await page.goto(str(server.make_url("/")))
        await page.get_by_role("heading", name="A little more in balance.").wait_for()
        output = Path("artifacts")
        output.mkdir(exist_ok=True)
        await page.screenshot(path=str(output / "mobile-overview.png"), full_page=True)
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert errors == []
        await browser.close()


@pytest.mark.parametrize("user_id", [11, 44])
async def test_mobile_member_wallet_setup(service, household, aiohttp_server, user_id):
    group, (admin, sister, husband, dan) = household
    guest = await service.add_guest(group["id"], 11, "Child")
    app = create_app(Settings(1, "test", TOKEN, "KinTabTestBot"), service, FakeTelegram(), QuietNotifier())
    server = await aiohttp_server(app)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=os.environ.get("KINTAB_CHROMIUM"))
        context = await browser.new_context(viewport={"width": 390, "height": 844})
        await context.route("https://telegram.org/**", lambda route: route.abort())
        await context.route("https://fonts.googleapis.com/**", lambda route: route.abort())
        await context.add_init_script(
            "window.Telegram={WebApp:{ready(){},expand(){},HapticFeedback:{notificationOccurred(){},selectionChanged(){}},"
            f"initData:{json.dumps(signed(user_id, start_param=group['token']))},"
            f"initDataUnsafe:{json.dumps({'start_param': group['token']})}" + "}};"
        )
        page = await context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(str(server.make_url("/")) + "#group")
        await page.get_by_role("heading", name="Manage shared wallets").wait_for()
        # Any joined member can set up a wallet they do not belong to.
        await page.get_by_label("Wallet to move").select_option(str(husband["wallet_id"]))
        await page.get_by_label("Combine into").select_option(str(sister["wallet_id"]))
        await page.get_by_role("button", name="Combine wallets", exact=True).click()
        settler = page.get_by_label("Settler for Sister & Husband", exact=True)
        await expect(settler).to_have_value(str(sister["id"]))
        await settler.select_option(str(husband["id"]))
        shared = page.locator(f'[data-wallet-id="{sister["wallet_id"]}"]')
        await shared.get_by_role("button", name="Save", exact=True).click()
        await expect(settler).to_have_value(str(husband["id"]))
        await expect(page.get_by_role("button", name="Combine wallets", exact=True)).to_be_enabled()
        state = await service.snapshot(group["id"])
        assert (
            next(w for w in state["wallets"] if w["id"] == sister["wallet_id"])["settler_member_id"]
            == husband["id"]
        )
        await page.get_by_label("Wallet to move").select_option(str(guest["wallet_id"]))
        await page.get_by_label("Combine into").select_option(str(sister["wallet_id"]))
        await page.get_by_role("button", name="Combine wallets", exact=True).click()
        await page.get_by_label("Settler for Sister & Husband & Child", exact=True).wait_for()
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        await page.get_by_role("button", name="Separate wallet for Husband", exact=True).click()
        await page.get_by_role("heading", name="Husband", exact=True).wait_for()
        await expect(page.get_by_label("Settler for Sister & Child", exact=True)).to_have_value(
            str(sister["id"])
        )
        await page.get_by_role("button", name="Separate wallet for Child", exact=True).click()
        await page.get_by_role("heading", name="Child", exact=True).wait_for()
        state = await service.snapshot(group["id"])
        assert next(m for m in state["members"] if m["id"] == guest["id"])["wallet_id"] != sister["wallet_id"]
        # Each wallet has an independently working settler form.
        await page.get_by_label("Settler for Athphane", exact=True).select_option(str(admin["id"]))
        await (
            page.locator(f'[data-wallet-id="{admin["wallet_id"]}"]')
            .get_by_role("button", name="Save", exact=True)
            .click()
        )
        await expect(page.get_by_role("button", name="Combine wallets", exact=True)).to_be_enabled()
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        # Wallet setup remains available to another regular member, alongside invitations.
        await page.evaluate(
            "async initData => { window.Telegram.WebApp.initData=initData; await refresh(); await renderPage(); }",
            signed(22, start_param=group["token"]),
        )
        await page.get_by_role("heading", name="A household, together.").wait_for()
        await page.get_by_role("heading", name="Manage shared wallets").wait_for()
        assert await page.locator(".settler-form").count() == 5
        assert await page.locator("#settings-form").count() == 1
        assert await page.get_by_role("button", name="Remove", exact=True).count() == 4
        assert await page.get_by_role("button", name="Delete tab…", exact=True).count() == 1
        await page.get_by_role("heading", name="Share a wallet", exact=True).wait_for()
        assert errors == []
        await browser.close()


async def test_mobile_member_onboarding_and_group_administration(service, household, aiohttp_server):
    group, (a, sister, husband, dan) = household
    gid = group["id"]
    server = await aiohttp_server(
        create_app(Settings(1, "test", TOKEN, "KinTabTestBot"), service, FakeTelegram(), QuietNotifier())
    )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=os.environ.get("KINTAB_CHROMIUM"))
        context = await browser.new_context(viewport={"width": 390, "height": 844})
        await context.route("https://telegram.org/**", lambda route: route.abort())
        await context.route("https://fonts.googleapis.com/**", lambda route: route.abort())
        await context.add_init_script(
            "window.Telegram={WebApp:{ready(){},expand(){},HapticFeedback:{notificationOccurred(){},selectionChanged(){}},"
            f"initData:{json.dumps(signed(22, start_param=group['token']))},"
            f"initDataUnsafe:{json.dumps({'start_param': group['token']})}" + "}};"
        )
        page = await context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(str(server.make_url("/")) + "#group")
        await page.get_by_role("heading", name="Add a member", exact=True).wait_for()
        await page.get_by_label("Member name", exact=True).fill("Child")
        await page.get_by_label("Starting wallet", exact=True).select_option(str(sister["wallet_id"]))
        await page.get_by_role("button", name="Add member", exact=True).click()
        await page.get_by_label("Settler for Sister & Child", exact=True).wait_for()
        state = await service.snapshot(gid)
        assert len(state["wallets"]) == 4
        child = next(m for m in state["members"] if m["name"] == "Child")
        await page.get_by_label("Member name", exact=True).fill("Parent")
        await page.get_by_role("button", name="Add member", exact=True).click()
        await page.get_by_role("heading", name="Parent", exact=True).wait_for()
        parent = next(m for m in (await service.snapshot(gid))["members"] if m["name"] == "Parent")
        assignment = page.locator("#assign-wallet-form")
        await assignment.get_by_label("Member", exact=True).select_option(str(parent["id"]))
        await assignment.get_by_label("Wallet", exact=True).select_option(str(sister["wallet_id"]))
        await page.get_by_role("button", name="Save wallet", exact=True).click()
        await page.get_by_label("Settler for Sister & Child & Parent", exact=True).wait_for()
        await assignment.get_by_label("Member", exact=True).select_option(str(child["id"]))
        await assignment.get_by_label("Wallet", exact=True).select_option("")
        await page.get_by_role("button", name="Save wallet", exact=True).click()
        await page.get_by_role("heading", name="Child", exact=True).wait_for()
        state = await service.snapshot(gid)
        assert (
            next(m for m in state["members"] if m["id"] == parent["id"])["wallet_id"] == sister["wallet_id"]
        )
        assert next(m for m in state["members"] if m["id"] == child["id"])["wallet_id"] != sister["wallet_id"]
        await page.get_by_label("Currency code", exact=True).fill("USD")
        await page.get_by_label("Decimal places", exact=True).select_option("0")
        await page.get_by_label("Default split", exact=True).select_option("wallet")
        await page.get_by_label("Pinned debts", exact=True).select_option("direct")
        await page.get_by_label("Weight for Child", exact=True).fill("0.5")
        await page.get_by_role("button", name="Save settings", exact=True).click()
        await expect(page.get_by_role("button", name="Save settings", exact=True)).to_be_enabled()
        state = await service.snapshot(gid)
        assert state["group"]["currency"] == "USD" and state["group"]["minor_units"] == 0
        assert state["group"]["split_mode"] == "wallet" and state["group"]["debt_mode"] == "direct"
        assert next(m for m in state["members"] if m["id"] == child["id"])["weight"] == "0.5"
        await service.add_expense(gid, 11, {"amount_minor": 600, "description": "Before removal"})
        before = await service.snapshot(gid)
        await (
            page.locator(f'[data-wallet-id="{dan["wallet_id"]}"]')
            .get_by_role("button", name="Remove", exact=True)
            .click()
        )
        await page.get_by_role("heading", name="Dan (left)", exact=True).wait_for()
        after = await service.snapshot(gid)
        assert after["balances"] == before["balances"] and after["expenses"] == before["expenses"]
        assert next(m for m in after["members"] if m["id"] == dan["id"])["left_at"]
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.once("dialog", lambda dialog: dialog.dismiss())
        await page.get_by_role("button", name="Delete tab…", exact=True).click()
        assert await service.group_for_token(group["token"])
        page.once("dialog", lambda dialog: dialog.accept())
        await page.get_by_role("button", name="Delete tab…", exact=True).click()
        await page.get_by_role("heading", name="Tab deleted", exact=True).wait_for()
        assert await service.group_for_token(group["token"]) is None
        assert errors == []
        await browser.close()
