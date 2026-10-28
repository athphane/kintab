"""Refresh README screenshots using fictional Maldivian members and temporary data.

Run from the project root: uv run python -m scripts.generate_demo_images
"""

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from bot.db import Database
from bot.groups import TabService
from bot.report import damage_image

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / "artifacts"
DESIGN = ARTIFACTS / "design"
SCREENSHOTS = ROOT / "docs/screenshots"
UI_IMAGES = [
    "kintab-mobile-light.png",
    "kintab-mobile-dark.png",
    "kintab-mobile-full.png",
    "kintab-mobile-expense.png",
    "kintab-mobile-history.png",
    "kintab-mobile-summary.png",
    "kintab-desktop-light.png",
    "kintab-desktop-dark.png",
]


async def report_previews():
    with TemporaryDirectory(prefix="kintab-demo-") as directory:
        db = await Database(Path(directory) / "demo.sqlite3").open()
        try:
            service = TabService(db)
            group = await service.ensure_group(-100123, "Demo household")
            gid = group["id"]
            people = [
                await service.join(gid, user_id, name, None)
                for user_id, name in [(11, "Aishath"), (22, "Ibrahim"), (33, "Fathimath"), (44, "Hassan")]
            ]
            await service.link_wallets(gid, 11, people[2]["wallet_id"], people[1]["wallet_id"])
            for user_id, amount, description, category in [
                (11, 90000, "Weekly groceries", "Groceries"),
                (22, 24000, "Electricity", "Utilities"),
            ]:
                await service.add_expense(
                    gid, user_id, {"amount_minor": amount, "description": description, "category": category}
                )
            for settled in [False, True]:
                state = await service.snapshot(gid)
                if settled:
                    for route in state["lines"]:
                        await service.add_transfer(gid, 11, route)
                    state = await service.snapshot(gid)
                suffix = "-settled" if settled else ""
                with damage_image(state) as output:
                    data = output.getvalue()
                for path in [
                    SCREENSHOTS / f"kintab-damage{suffix}.png",
                    DESIGN / f"damage{suffix}-preview-v2.png",
                    ARTIFACTS / f"damage{suffix}-preview.png",
                ]:
                    path.write_bytes(data)
        finally:
            await db.close()


def main():
    DESIGN.mkdir(parents=True, exist_ok=True)
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_browser.py::test_responsive_design_and_theme", "-q"],
        cwd=ROOT,
        env={**os.environ, "KINTAB_BROWSER_TEST": "1"},
        check=True,
    )
    for name in UI_IMAGES:
        shutil.copyfile(DESIGN / name, SCREENSHOTS / name)
    asyncio.run(report_previews())
    print(f"Updated {len(UI_IMAGES) + 2} README images with fictional Maldivian demo data.")


if __name__ == "__main__":
    main()
