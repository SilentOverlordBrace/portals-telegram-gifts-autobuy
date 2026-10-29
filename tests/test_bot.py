import asyncio

from portals_autobuy.bot import BOT_COMMANDS, HELP, AppContext, build_dispatcher, parse_on_off
from portals_autobuy.client import MockClient
from portals_autobuy.config import Settings
from portals_autobuy.engine import Engine
from portals_autobuy.notifier import MemoryNotifier
from portals_autobuy.storage import Storage

SHARED = ("/why", "/panic", "/safety", "/doctor", "/scoreboard")


def test_parse_on_off():
    assert parse_on_off("on") is True
    assert parse_on_off(" OFF ") is False
    assert parse_on_off(None) is None
    assert parse_on_off("maybe") is None


def test_help_lists_all_commands():
    for cmd in ("/status", "/pause", "/resume", "/rules", "/addrule", "/delrule", "/dry", "/balance", "/buys",
                "/floor", "/hunt", "/mode", "/pnl", "/score", "/sold", "/export", "/preset", "/peers", "/compare",
                "/digest", "/mute", "/promote", *SHARED):
        assert cmd in HELP, cmd
    menu = {c.command for c in BOT_COMMANDS}
    assert {s.lstrip("/") for s in SHARED} <= menu


def test_dispatcher_builds():
    async def go():
        settings = Settings(_env_file=None, ADMIN_IDS="1")
        storage = await Storage(":memory:").open()
        try:
            client = MockClient()
            engine = Engine(settings, storage, client, MemoryNotifier())
            dp = build_dispatcher(AppContext(settings, storage, client, engine))
            assert dp["ctx"].client is client
        finally:
            await storage.close()

    asyncio.run(go())
