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


@pytest.mark.parametrize("telegram_context", ["none", "browser", "miniapp"])
async def test_public_onboarding_and_telegram_handoff(service, household, aiohttp_server, telegram_context):
    group, _ = household
    server = await aiohttp_server(
        create_app(Settings(1, "test", TOKEN, "KinTabTestBot"), service, FakeTelegram(), QuietNotifier())
    )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=os.environ.get("KINTAB_CHROMIUM"))
        context = await browser.new_context(viewport={"width": 390, "height": 844})
        await context.route("https://telegram.org/**", lambda route: route.abort())
        await context.route("https://fonts.googleapis.com/**", lambda route: route.abort())
        await context.route("https://t.me/**", lambda route: route.fulfill(body="Telegram handoff"))
        if telegram_context != "none":
            init_data = "expired-session" if telegram_context == "miniapp" else ""
            await context.add_init_script(
                "window.Telegram={WebApp:{ready(){},expand(){},"
                "openTelegramLink(url){window.openedTelegramLink=url;},"
                f"initData:{json.dumps(init_data)},"
                f"initDataUnsafe:{json.dumps({'start_param': group['token']})}" + "}};"
            )
        page = await context.new_page()
        errors, api_requests = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request", lambda request: api_requests.append(request.url) if "/api/" in request.url else None
        )
        await page.goto(str(server.make_url("/")) + "#history")
        await page.get_by_role("heading", name="Your household tab.").wait_for()
        add = page.get_by_role("link", name="Add KinTab to your group", exact=True)
        expected_url = "https://t.me/KinTabTestBot?startgroup=welcome"
        await expect(add).to_have_attribute("href", expected_url)
        await expect(page.get_by_role("link", name="Open KinTab in Telegram")).to_have_attribute(
            "href", "https://t.me/KinTabTestBot"
        )
        await expect(page.get_by_role("navigation")).to_be_hidden()
        await expect(page.locator(".onboarding-steps li")).to_have_count(3)
        if telegram_context == "miniapp":
            await expect(page.get_by_role("alert")).to_have_text("Reopen the tab from Telegram to sign in.")
            assert len(api_requests) == 1
        else:
            assert api_requests == []
        if telegram_context == "browser":
            output = Path("artifacts/design")
            output.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(output / "kintab-onboarding.png"), full_page=True)
        await page.set_viewport_size({"width": 320, "height": 844})
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        await add.click()
        if telegram_context == "miniapp":
            assert await page.evaluate("window.openedTelegramLink") == expected_url
            assert page.url.endswith("#history")
        else:
            await page.wait_for_url(expected_url)
        assert errors == []
        await browser.close()


async def test_responsive_design_and_theme(service, household, aiohttp_server):
    group, people = household
    for payer, amount, description, category in [
        (11, 90000, "Weekly groceries", "Groceries"),
        (22, 65000, "Electricity", "Utilities"),
        (44, 18000, "Dinner together", "Eating out"),
    ]:
        await service.add_expense(
            group["id"],
            payer,
            {"amount_minor": amount, "description": description, "category": category},
        )
    server = await aiohttp_server(
        create_app(Settings(1, "test", TOKEN, "KinTabTestBot"), service, FakeTelegram(), QuietNotifier())
    )
    output = Path("artifacts/design")
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=os.environ.get("KINTAB_CHROMIUM"))
        context = await browser.new_context(viewport={"width": 390, "height": 844})
        await context.route("https://telegram.org/**", lambda route: route.abort())
        await context.route("https://fonts.googleapis.com/**", lambda route: route.abort())
        await context.add_init_script(
            "window.Telegram={WebApp:{ready(){},expand(){},colorScheme:'light',"
            "onEvent(name,callback){this[name]=callback;},"
            f"initData:{json.dumps(signed(start_param=group['token']))},"
            f"initDataUnsafe:{json.dumps({'start_param': group['token']})}" + "}};"
        )
        page = await context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(str(server.make_url("/")))
        await page.get_by_role("heading", name="A little more in balance.").wait_for()
        artwork = page.locator(".balance-art")
        await expect(artwork).to_have_js_property("complete", True)
        assert await artwork.evaluate("image => image.naturalWidth > 0")
        await page.screenshot(path=str(output / "kintab-mobile-light.png"))
        await page.screenshot(path=str(output / "kintab-mobile-full.png"), full_page=True)
        for scheme in ["light", "dark"]:
            await page.evaluate(
                "scheme => { window.Telegram.WebApp.colorScheme=scheme; window.Telegram.WebApp.themeChanged(); }",
                scheme,
            )
            for width in [320, 390, 768, 1280]:
                await page.set_viewport_size({"width": width, "height": 900})
                for route, title in [
                    ("home", "A little more in balance."),
                    ("history", "The whole picture."),
                    ("expense", "Someone picked up the bill."),
                    ("summary", "A month, accounted for."),
                    ("group", "A household, together."),
                    ("settle", "Bring things even."),
                    ("transfer:payment", "Record a payment."),
                    ("breakdown", "Every part of the balance."),
                ]:
                    await page.evaluate("route => location.hash=route", route)
                    await page.get_by_role("heading", name=title, exact=True).wait_for()
                    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
                        scheme,
                        width,
                        route,
                    )
                    active = page.locator('nav [aria-current="page"]')
                    await expect(active).to_have_count(1)
                    if scheme == "light" and width == 390 and route in ["expense", "history", "summary"]:
                        await page.screenshot(path=str(output / f"kintab-mobile-{route}.png"), full_page=True)
                await page.evaluate("location.hash='home'")
                await page.get_by_role("heading", name="A little more in balance.").wait_for()
                if width == 1280:
                    await page.screenshot(path=str(output / f"kintab-desktop-{scheme}.png"), full_page=True)
                if scheme == "dark" and width == 390:
                    await page.screenshot(path=str(output / "kintab-mobile-dark.png"))
        assert errors == []
        await browser.close()


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
        await page.get_by_label("Member name", exact=True).fill("Ismail")
        await page.get_by_role("button", name="Add member", exact=True).click()
        await page.get_by_role("heading", name="A household, together.").wait_for()
        await page.get_by_role("heading", name="Ismail", exact=True).wait_for()
        await page.goto(str(server.make_url("/")) + "#expense")
        await page.get_by_label("Amount · MVR").fill("100")
        await page.get_by_label("Description", exact=True).fill('<img src=x onerror="window.XSS=true">')
        await page.get_by_label("Split between").select_option("custom")
        await page.locator('input[name="participant"]').nth(2).uncheck()
        await page.locator('input[name="participant"]').nth(3).uncheck()
        await page.locator('input[name="participant"]').nth(4).uncheck()
        await page.get_by_label("Weight for Ibrahim").fill("0.5")
        await page.get_by_role("button", name="Add expense", exact=True).click()
        await page.get_by_role("heading", name="The whole picture.").wait_for()
        assert not await page.evaluate("Boolean(window.XSS)")
        state = await service.snapshot(group["id"])
        assert [s["amount_minor"] for s in state["expenses"][1]["shares"]] == [6667, 3333]
        await (
            page.locator("article.entry")
            .filter(has_text="<img src=x")
            .get_by_role("link", name="Edit")
            .click()
        )
        await expect(page.get_by_text("Current shares")).to_be_visible()
        await expect(page.get_by_label("Change who shares this expense")).to_be_checked()
        await expect(page.get_by_label("Split between")).to_be_visible()
        await expect(page.get_by_label("Split between")).to_have_value("custom")
        await expect(page.get_by_label("Weight for Ibrahim")).to_have_value("0.5")
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
    group, (admin, ibrahim, fathimath, hassan) = household
    guest = await service.add_guest(group["id"], 11, "Mariyam")
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
        await page.get_by_label("Wallet to move").select_option(str(fathimath["wallet_id"]))
        await page.get_by_label("Combine into").select_option(str(ibrahim["wallet_id"]))
        await page.get_by_role("button", name="Combine wallets", exact=True).click()
        settler = page.get_by_label("Settler for Ibrahim & Fathimath", exact=True)
        await expect(settler).to_have_value(str(ibrahim["id"]))
        await settler.select_option(str(fathimath["id"]))
        shared = page.locator(f'[data-wallet-id="{ibrahim["wallet_id"]}"]')
        await shared.get_by_role("button", name="Save", exact=True).click()
        await expect(settler).to_have_value(str(fathimath["id"]))
        await expect(page.get_by_role("button", name="Combine wallets", exact=True)).to_be_enabled()
        state = await service.snapshot(group["id"])
        assert (
            next(w for w in state["wallets"] if w["id"] == ibrahim["wallet_id"])["settler_member_id"]
            == fathimath["id"]
        )
        await page.get_by_label("Wallet to move").select_option(str(guest["wallet_id"]))
        await page.get_by_label("Combine into").select_option(str(ibrahim["wallet_id"]))
        await page.get_by_role("button", name="Combine wallets", exact=True).click()
        await page.get_by_label("Settler for Ibrahim & Fathimath & Mariyam", exact=True).wait_for()
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        await page.get_by_role("button", name="Separate wallet for Fathimath", exact=True).click()
        await page.get_by_role("heading", name="Fathimath", exact=True).wait_for()
        await expect(page.get_by_label("Settler for Ibrahim & Mariyam", exact=True)).to_have_value(
            str(ibrahim["id"])
        )
        await page.get_by_role("button", name="Separate wallet for Mariyam", exact=True).click()
        await page.get_by_role("heading", name="Mariyam", exact=True).wait_for()
        state = await service.snapshot(group["id"])
        assert (
            next(m for m in state["members"] if m["id"] == guest["id"])["wallet_id"] != ibrahim["wallet_id"]
        )
        # Each wallet has an independently working settler form.
        await page.get_by_label("Settler for Aishath", exact=True).select_option(str(admin["id"]))
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
    group, (a, ibrahim, fathimath, hassan) = household
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
        await page.get_by_label("Member name", exact=True).fill("Mariyam")
        await page.get_by_label("Starting wallet", exact=True).select_option(str(ibrahim["wallet_id"]))
        await page.get_by_role("button", name="Add member", exact=True).click()
        await page.get_by_label("Settler for Ibrahim & Mariyam", exact=True).wait_for()
        state = await service.snapshot(gid)
        assert len(state["wallets"]) == 4
        child = next(m for m in state["members"] if m["name"] == "Mariyam")
        await page.get_by_label("Member name", exact=True).fill("Ismail")
        await page.get_by_role("button", name="Add member", exact=True).click()
        await page.get_by_role("heading", name="Ismail", exact=True).wait_for()
        parent = next(m for m in (await service.snapshot(gid))["members"] if m["name"] == "Ismail")
        assignment = page.locator("#assign-wallet-form")
        await assignment.get_by_label("Member", exact=True).select_option(str(parent["id"]))
        await assignment.get_by_label("Wallet", exact=True).select_option(str(ibrahim["wallet_id"]))
        await page.get_by_role("button", name="Save wallet", exact=True).click()
        await page.get_by_label("Settler for Ibrahim & Mariyam & Ismail", exact=True).wait_for()
        await assignment.get_by_label("Member", exact=True).select_option(str(child["id"]))
        await assignment.get_by_label("Wallet", exact=True).select_option("")
        await page.get_by_role("button", name="Save wallet", exact=True).click()
        await page.get_by_role("heading", name="Mariyam", exact=True).wait_for()
        state = await service.snapshot(gid)
        assert (
            next(m for m in state["members"] if m["id"] == parent["id"])["wallet_id"] == ibrahim["wallet_id"]
        )
        assert (
            next(m for m in state["members"] if m["id"] == child["id"])["wallet_id"] != ibrahim["wallet_id"]
        )
        await page.get_by_label("Currency code", exact=True).fill("USD")
        await page.get_by_label("Decimal places", exact=True).select_option("0")
        await page.get_by_label("Default split", exact=True).select_option("wallet")
        await page.get_by_label("Pinned debts", exact=True).select_option("direct")
        await page.get_by_label("Weight for Mariyam", exact=True).fill("0.5")
        await page.get_by_role("button", name="Save settings", exact=True).click()
        await expect(page.get_by_role("button", name="Save settings", exact=True)).to_be_enabled()
        state = await service.snapshot(gid)
        assert state["group"]["currency"] == "USD" and state["group"]["minor_units"] == 0
        assert state["group"]["split_mode"] == "wallet" and state["group"]["debt_mode"] == "direct"
        assert next(m for m in state["members"] if m["id"] == child["id"])["weight"] == "0.5"
        await service.add_expense(gid, 11, {"amount_minor": 600, "description": "Before removal"})
        before = await service.snapshot(gid)
        await (
            page.locator(f'[data-wallet-id="{hassan["wallet_id"]}"]')
            .get_by_role("button", name="Remove", exact=True)
            .click()
        )
        await page.get_by_role("heading", name="Hassan (left)", exact=True).wait_for()
        after = await service.snapshot(gid)
        assert after["balances"] == before["balances"] and after["expenses"] == before["expenses"]
        assert next(m for m in after["members"] if m["id"] == hassan["id"])["left_at"]
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
