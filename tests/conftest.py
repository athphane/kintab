import pytest

from bot.db import Database
from bot.groups import TabService


@pytest.fixture
async def service(tmp_path):
    db = await Database(tmp_path / "tab.sqlite3").open()
    yield TabService(db)
    await db.close()


@pytest.fixture
async def household(service):
    group = await service.ensure_group(-100123, "Household")
    people = [
        await service.join(group["id"], uid, name, username)
        for uid, name, username in [
            (11, "Athphane", "athphane"),
            (22, "Sister", "sister"),
            (33, "Husband", None),
            (44, "Dan", "dan"),
        ]
    ]
    return group, people
