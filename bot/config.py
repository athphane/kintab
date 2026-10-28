import os
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    bot_token: str
    bot_username: str
    app_name: str = "app"
    public_url: str = "https://kintab.athfan.dev"
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    data_dir: Path = BASE / "data"
    workdir: Path = BASE / "workdir"

    def mini_app_url(self, token: str) -> str:
        return f"https://t.me/{self.bot_username}/{self.app_name}?startapp={token}"


def load_settings(path: str | None = None) -> Settings:
    config = ConfigParser(interpolation=None)
    filename = Path(path or os.environ.get("KINTAB_CONFIG", BASE / "config.ini"))
    if not config.read(filename):
        raise ValueError("Copy config.ini.example to config.ini and fill in the Telegram credentials.")
    tg = config["telegram"]
    settings = Settings(
        api_id=tg.getint("api_id"),
        api_hash=tg["api_hash"].strip(),
        bot_token=tg["bot_token"].strip(),
        bot_username=tg["bot_username"].strip().lstrip("@"),
        app_name=tg.get("app_name", "app").strip(),
        public_url=config.get("web", "public_url", fallback="https://kintab.athfan.dev").rstrip("/"),
        web_host=os.environ.get("KINTAB_WEB_HOST") or config.get("web", "host", fallback="127.0.0.1"),
        web_port=config.getint("web", "port", fallback=8080),
        data_dir=BASE / config.get("storage", "data_dir", fallback="data"),
        workdir=BASE / config.get("storage", "workdir", fallback="workdir"),
    )
    if not all((settings.api_id, settings.api_hash, settings.bot_token, settings.bot_username)):
        raise ValueError("All Telegram credentials and bot_username are required.")
    if not settings.public_url.startswith("https://"):
        raise ValueError("public_url must use HTTPS.")
    if not all(c.isalnum() or c == "_" for c in settings.bot_username + settings.app_name):
        raise ValueError("Invalid bot username or Mini App short name.")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.workdir.mkdir(parents=True, exist_ok=True)
    return settings
