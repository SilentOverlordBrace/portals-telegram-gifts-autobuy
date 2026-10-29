"""Regression tests for the v0.1.0 release check: an honest /resume answer inside a LIVE window, one-time resume
buttons, NaN limits that fail closed, `mock` exiting on a bad BOT_TOKEN, /start onboarding, a UTC default with
labelled times, plain words in user-visible text, singular/plural, accurate help text, the near-miss card, the
ask-first threshold under a /preset and removed scaffold code."""

import asyncio
import inspect
import math
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from aiogram.filters import Command
from helpers import ADMIN, NOW, Clock, L, demo_engine, settings
from pydantic import ValidationError

from portals_autobuy import bot as bot_mod
from portals_autobuy import client as client_mod
from portals_autobuy import doctor, engine as engine_mod, explain, notifier as notifier_mod, rules as rules_mod
from portals_autobuy.__main__ import build_parser
from portals_autobuy.bot import HELP, AppContext, build_router, mute_key
from portals_autobuy.callbacks import GateCB, ResumeCB, RuleCB, RuleResumeCB
from portals_autobuy.client import DEMO_RULES, MockClient
from portals_autobuy.config import Settings
from portals_autobuy.digest import build_digest
from portals_autobuy.engine import Limits, check_limits_detail
from portals_autobuy.notifier import keyboard_labels, render_near_miss
from portals_autobuy.pricing import FeeModel, FloorEstimate, assess, deal_summary
from portals_autobuy.rules import BuyRule, NearMiss, RuleParseError, parse_addrule_args
from portals_autobuy.safety import safety_text

ROOT = Path(__file__).resolve().parents[1]
LIVE = dict(DRY_RUN=False)


# --- a tiny driver for the real aiogram handlers ------------------------------------------------------------


class _User:
    id = ADMIN


class _Chat:
    id = ADMIN


class FakeMsg:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.from_user = _User()
        self.chat = _Chat()
        self.message_id = 1
        self.reply_to_message = None
        self.out: list[tuple[str, object]] = []

    async def answer(self, text, reply_markup=None, **kw):
        self.out.append((text, reply_markup))


class FakeCmd:
    def __init__(self, args):
        self.args = args


class FakeState:
    def __init__(self):
        self.data = {}

    async def set_state(self, s):
        pass

    async def set_data(self, d):
        self.data = d

    async def get_data(self):
        return self.data

    async def update_data(self, **kw):
        self.data.update(kw)


class FakeCB:
    def __init__(self):
        self.from_user = _User()
        self.message = None
        self.answers: list[str] = []

    async def answer(self, text="", show_alert=False):
        self.answers.append(text)


def _handlers(router):
    cmds, cbs = {}, {}
    for h in router.message.handlers:
        for f in h.filters or []:
            if isinstance(f.callback, Command):
                for c in f.callback.commands:
                    cmds[c] = h.callback
    for h in router.callback_query.handlers:
        for f in h.filters or []:
            data = getattr(f.callback, "callback_data", None)
            if data is not None:
                cbs[data.__name__] = h.callback
    return cmds, cbs


class Bot:
    """The admin router of one engine, driven with fake messages and button presses."""

    def __init__(self, engine, storage, client):
        self.ctx = AppContext(engine.settings, storage, client, engine)
        self.cmds, self.cbs = _handlers(build_router(self.ctx))

    async def send(self, name: str, args: str | None = None) -> list[tuple[str, object]]:
        fn = self.cmds[name]
        msg = FakeMsg(f"/{name} {args or ''}")
        params = inspect.signature(fn).parameters
        kw = {}
        if "command" in params:
            kw["command"] = FakeCmd(args)
        if "state" in params:
            kw["state"] = FakeState()
        await fn(msg, **kw)
        return msg.out

    async def press(self, data) -> list[str]:
        cb = FakeCB()
        await self.cbs[type(data).__name__](cb, data)
        return cb.answers


def _button(markup, cls):
    """The first button of the keyboard whose callback data is a `cls`, unpacked."""
    for row in markup.inline_keyboard:
        for b in row:
            if b.callback_data and b.callback_data.startswith(cls.__prefix__ + ":"):
                return cls.unpack(b.callback_data)
    raise AssertionError(f"no {cls.__name__} button")


# --- high: /resume inside an armed LIVE window says LIVE, never "still practice" ---------------------------


def test_resume_after_pause_in_a_live_window_says_live_and_buys_live():
    async def go():
        engine, storage, client, _, _ = await demo_engine(settings(**LIVE), force_dry=False)
        b = Bot(engine, storage, client)
        try:
            assert await engine.arm_live() is None
            await b.send("pause")
            [(prompt, kb)] = await b.send("resume")
            assert "LIVE is still armed until" in prompt and "UTC" in prompt and "real buys" in prompt
            answers = await b.press(_button(kb, ResumeCB))
            assert answers and "Mode: LIVE until" in answers[0] and "real TON" in answers[0]
            assert not any("practice until" in a.lower() or "still practice" in a.lower() for a in answers)
            assert not await engine.is_paused() and not await engine.is_dry()
            await engine.run_cycle()
            assert client.bought  # it really is live, and the bot said so
        finally:
            await storage.close()

    asyncio.run(go())


def test_resume_after_panic_says_practice_until_dry_off():
    async def go():
        engine, storage, client, _, _ = await demo_engine(settings(**LIVE), force_dry=False)
        b = Bot(engine, storage, client)
        try:
            await engine.arm_live()
            await b.send("panic")
            [(prompt, kb)] = await b.send("resume")
            assert "/panic is on" in prompt
            [answer] = await b.press(_button(kb, ResumeCB))
            assert answer.startswith("Resumed. Mode: PRACTICE until a new /dry off")
            assert await engine.is_dry() and not await engine.is_panic()
        finally:
            await storage.close()

    asyncio.run(go())


def test_balance_mismatch_card_warns_that_resume_continues_live():
    class Drained(MockClient):
        async def balance(self):
            return 900.0  # 100 TON gone that we never spent

    async def go():
        clock = Clock()
        engine, storage, client, notifier, _ = await demo_engine(settings(**LIVE), force_dry=False, clock=clock,
                                                                 client=Drained(now=clock()))
        b = Bot(engine, storage, client)
        try:
            await engine.arm_live()
            await storage.set_setting("balance_last", 1000.0)
            await storage.set_setting("balance_ts", clock() - 60)
            await engine.run_cycle()
            [rec] = [r for r in notifier.records if "someone else may be using this account" in r["text"]]
            assert "LIVE is still armed: Resume continues real buys" in rec["text"]
            answers = await b.press(_button(rec["keyboard"], ResumeCB))
            assert answers[0].startswith("Resumed. Mode: LIVE")
        finally:
            await storage.close()

    asyncio.run(go())


# --- medium: resume buttons are one-time and a newer stop voids them -------------------------------------


def test_old_resume_button_cannot_undo_a_newer_panic():
    async def go():
        engine, storage, client, _, _ = await demo_engine()
        b = Bot(engine, storage, client)
        try:
            [(_, kb)] = await b.send("resume")
            old = _button(kb, ResumeCB)
            await b.send("panic")
            [answer] = await b.press(old)
            assert "out of date" in answer
            assert await engine.is_panic() and await engine.is_paused()
            # the pre-nonce button shape changes nothing either
            assert "out of date" in (await b.press(GateCB(action="resume")))[0]
            assert await engine.is_panic()
            # a fresh /resume works, once
            [(_, kb)] = await b.send("resume")
            fresh = _button(kb, ResumeCB)
            assert (await b.press(fresh))[0].startswith("Resumed.")
            assert not await engine.is_panic() and not await engine.is_paused()
            await b.send("pause")
            assert "out of date" in (await b.press(fresh))[0] and await engine.is_paused()
        finally:
            await storage.close()

    asyncio.run(go())


def test_resume_nonce_expires():
    async def go():
        engine, storage, _, _, clock = await demo_engine()
        try:
            await engine.set_paused(True)
            nonce = await engine.issue_resume_nonce()
            clock.advance(engine_mod.RESUME_TTL_SEC + 1)
            assert not await engine.resume_with(nonce) and await engine.is_paused()
        finally:
            await storage.close()

    asyncio.run(go())


def test_old_crash_card_cannot_undo_a_newer_crash_pause():
    async def go():
        engine, storage, client, notifier, clock = await demo_engine()
        b = Bot(engine, storage, client)
        rule = (await storage.list_rules())[1]  # Lunar Snake / Cobra
        try:
            async def crash():
                key = rule.filter_key_str()
                await storage.add_snapshot(clock() - 1800, key, 1.0, 4, "cluster")
                await storage.add_snapshot(clock() - 60, key, 0.5, 4, "cluster")
                await storage.delete_setting(f"crash_alert:{rule.id}")
                await engine._crash_breaker([rule], {key: [(clock() - 1800, 1.0), (clock() - 60, 0.5)]}, clock())
                [rec] = [r for r in notifier.records if r["text"].startswith(f"Rule #{rule.id} paused")][-1:]
                return _button(rec["keyboard"], RuleResumeCB)

            first = await crash()
            await storage.delete_setting(f"rule_paused_until:{rule.id}")
            clock.advance(60)
            second = await crash()  # a newer pause of the same rule
            assert "out of date" in (await b.press(first))[0]
            assert await storage.get_float(f"rule_paused_until:{rule.id}") is not None
            assert "out of date" in (await b.press(RuleCB(action="resume", rule_id=rule.id)))[0]  # legacy shape
            assert await storage.get_float(f"rule_paused_until:{rule.id}") is not None
            assert (await b.press(second))[0] == f"Rule #{rule.id} resumed"
            assert await storage.get_float(f"rule_paused_until:{rule.id}") is None
            assert "out of date" in (await b.press(second))[0]  # one-time
        finally:
            await storage.close()

    asyncio.run(go())


def test_new_callback_data_fits_64_bytes():
    datas = [ResumeCB(nonce="A" * 8).pack(), RuleResumeCB(rule_id=10**9, nonce="A" * 8).pack()]
    assert all(len(d.encode()) <= 64 for d in datas)


# --- medium: limits that are not finite fail closed ---------------------------------------------------------


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
@pytest.mark.parametrize("key", ["MAX_PRICE_TON", "DAILY_BUDGET_TON", "CONFIRM_ABOVE_TON"])
def test_settings_refuse_non_finite_numbers(key, value):
    with pytest.raises(ValidationError, match="finite"):
        Settings(_env_file=None, **{key: value})


def test_cli_names_the_key_for_a_nan_limit(tmp_path):
    env = {**os.environ, "MAX_PRICE_TON": "nan", "PYTHONPATH": str(ROOT)}
    out = subprocess.run([sys.executable, "-m", "portals_autobuy", "doctor"], cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 2
    assert "MAX_PRICE_TON: Input should be a finite number" in out.stderr
    assert "✓ Limits" not in out.stdout


@pytest.mark.parametrize("limits", [Limits(math.nan, 50, 3), Limits(10, math.nan, 3)])
def test_limit_check_blocks_on_nan(limits):
    assert check_limits_detail(1.0, limits, spent_24h=0.0, buys_last_hour=0) is not None
    assert check_limits_detail(math.nan, Limits(10, 50, 3), spent_24h=0.0, buys_last_hour=0) is not None


def test_nan_preset_override_is_ignored():
    async def go():
        engine, storage, *_ = await demo_engine()
        try:
            await storage.set_setting("limit:MAX_PRICE_TON", "nan")
            assert (await engine.effective_limits()).max_price_ton == 10
        finally:
            await storage.close()

    asyncio.run(go())


@pytest.mark.parametrize("args", ["Spy Agaric max=nan", "Spy Agaric max=inf", "Spy Agaric max=5 discount=nan",
                                  "Spy Agaric max=5 profit=nan", "Spy Agaric max=5 locked=inf"])
def test_addrule_refuses_non_finite_numbers(args):
    with pytest.raises(RuleParseError, match="finite"):
        parse_addrule_args(args)


@pytest.mark.parametrize("key", ["min_discount_pct", "min_profit_ton", "min_profit_pct", "allow_locked_days"])
def test_rules_json_refuses_non_finite_numbers(key):
    with pytest.raises(RuleParseError, match="finite"):
        BuyRule.from_dict({"collection": "Spy Agaric", "max_price_ton": 5, key: "nan"})


def test_doctor_fails_on_a_nan_limit():
    s = Settings.model_construct(**{**settings().model_dump(), "MAX_PRICE_TON": math.nan})
    check = asyncio.run(doctor.check_limits(doctor.DoctorDeps(s)))
    assert check.status == "fail" and "MAX_PRICE_TON" in check.text and check.fix


# --- medium: `mock` exits on a malformed BOT_TOKEN or ADMIN_IDS instead of hanging ------------------------


@pytest.mark.parametrize("env,key", [({"BOT_TOKEN": "bad"}, "BOT_TOKEN"),
                                     ({"BOT_TOKEN": "123456:" + "A" * 35, "ADMIN_IDS": "abc"}, "ADMIN_IDS")])
def test_mock_exits_on_a_bad_bot_config(tmp_path, env, key):
    full = {**os.environ, **env, "PYTHONPATH": str(ROOT)}
    out = subprocess.run([sys.executable, "-m", "portals_autobuy", "mock", "--cycles", "1"], cwd=tmp_path,
                         env=full, capture_output=True, text=True, timeout=60)  # used to hang forever
    assert out.returncode == 2
    assert key in out.stderr


# --- medium: /start onboards a stranger -------------------------------------------------------------------


def test_start_says_what_the_bot_does_and_names_three_commands():
    async def go():
        engine, storage, client, _, _ = await demo_engine(rules=[])
        b = Bot(engine, storage, client)
        try:
            out = await b.send("start")
            text = out[0][0]
            assert text.startswith("Portals Autobuy watches the Portals marketplace")
            assert "practice mode" in text and "Mode: PRACTICE" in text
            tries = text.split("Try these first:")[1]
            assert [c for c in ("/hunt", "/scoreboard", "/doctor") if c in tries] == ["/hunt", "/scoreboard", "/doctor"]
            assert "/addrule <collection>" not in text  # the full reference is /help
            assert "no rules yet" in text and len(out) > 1  # then the /hunt wizard
            help_text = (await b.send("help"))[0][0]
            assert "/addrule <collection>" in help_text
        finally:
            await storage.close()

    asyncio.run(go())


# --- medium: UTC by default, and every time names its zone ----------------------------------------------


def test_default_time_zone_is_utc_and_times_are_labelled():
    assert Settings(_env_file=None).TZ_OFFSET_HOURS == 0
    assert explain.tz_label(0) == "UTC" and explain.tz_label(3) == "UTC+3" and explain.tz_label(-5) == "UTC-5"

    async def go():
        engine, storage, client, _, clock = await demo_engine()
        try:
            await engine.run_cycle()
            utc = explain.why_text(await storage.decisions_for("ls-4412"), tz_offset_hours=0)
            msk = explain.why_text(await storage.decisions_for("ls-4412"), tz_offset_hours=3)
            assert "(times in UTC)" in utc and "(times in UTC+3)" in msk
            assert explain.local_time(clock(), 0) in utc and explain.local_time(clock(), 3) in msk
        finally:
            await storage.close()

    asyncio.run(go())


def test_live_until_and_buys_show_the_zone():
    async def go():
        engine, storage, client, _, _ = await demo_engine(settings(**LIVE, TZ_OFFSET_HOURS=3), force_dry=False)
        b = Bot(engine, storage, client)
        try:
            await engine.arm_live()
            text = safety_text(await engine.safety_state(), engine.settings)
            assert "Mode: LIVE until" in text and "UTC+3, practice after that" in text
            await engine.run_cycle()
            buys = (await b.send("buys"))[0][0]
            assert buys.startswith("Last purchases (times in UTC+3):")
        finally:
            await storage.close()

    asyncio.run(go())


# --- low: no internal names in user-visible text --------------------------------------------------------


def test_no_internal_names_in_cli_help_and_bot_text():
    parser = build_parser()
    sub = next(a for a in parser._actions if a.dest == "command")
    texts = [parser.format_help()] + [p.format_help() for p in sub.choices.values()]
    texts += [HELP]
    for t in texts:
        for word in ("FakeTapper", "sibling", "Sibling", "suite", "Suite", "F2"):
            assert word not in t, (word, t[:60])


def test_peers_and_digest_use_plain_words(tmp_path):
    async def go():
        peer = tmp_path / "events.jsonl"
        peer.write_text('{"v": 1, "ts": %f, "bot": "mrkt-autobuy", "kind": "skip", "mode": "dry", "id": "x", '
                        '"subject": {"type": "gift", "ref": "x"}, "reasons": []}\n' % (NOW - 60))
        engine, storage, *_ = await demo_engine(settings(PEER_EVENT_FILES=str(peer)))
        try:
            digest = await build_digest(engine, NOW - 86400, NOW)
            assert "Other bots (PEER_EVENT_FILES):" in digest and "Suite" not in digest
        finally:
            await storage.close()
        from portals_autobuy.peers import Peers

        lines = await Peers(settings(), "portals").status_lines()
        assert "Other markets: off (PEER_FLOOR_FILES is empty)" in lines

    asyncio.run(go())


# --- low: singular and plural ---------------------------------------------------------------------------


def test_one_comparable_is_singular():
    one = assess(L("x", 3.0), FloorEstimate(4.0, "thin", 1, "low"), FeeModel(sell_pct=5, withdraw_ton=0.1), [])
    assert "(thin, 1 comparable, low)" in deal_summary("Cake", one)
    assert FloorEstimate(4.0, "thin", 1, "low").label() == "4.00 (thin, 1 comparable, low)"
    assert FloorEstimate(4.0, "thin", 3, "low").label() == "4.00 (thin, 3 comparables, low)"
    assert explain.plural(1, "comparable listing") == "1 comparable listing"
    assert explain.plural(2, "comparable listing") == "2 comparable listings"


def test_digest_tuning_hint_singular():
    async def go():
        engine, storage, *_ = await demo_engine(rules=[])
        try:
            for i, profit in enumerate((0.3, -0.1, -0.2)):
                await storage.add_decision(NOW - 60, f"z{i}", 2.6, 3, "skip", "OVER_MAX",
                                           {"text": "x", "profit_ton": profit})
            digest = await build_digest(engine, NOW - 86400, NOW + 1)
            assert "Tuning hint: 3 deals were skipped by OVER_MAX" in digest
            assert "and 1 of them was +net at the floor (+0.30 TON)" in digest
            await storage.add_decision(NOW - 30, "z9", 2.6, 3, "skip", "OVER_MAX", {"text": "x", "profit_ton": 0.1})
            assert "and 2 of them were +net" in await build_digest(engine, NOW - 86400, NOW + 1)
        finally:
            await storage.close()

    asyncio.run(go())


def test_doctor_bot_config_singular_fix():
    check = asyncio.run(doctor.check_bot(doctor.DoctorDeps(settings(BOT_TOKEN=""))))
    assert check.status == "fail" and check.fix.startswith("Add it to .env")
    both = asyncio.run(doctor.check_bot(doctor.DoctorDeps(settings(BOT_TOKEN="", ADMIN_IDS=""))))
    assert both.fix.startswith("Add them to .env")


# --- low: help text says what /dry off really does --------------------------------------------------------


def test_help_and_safety_do_not_claim_dry_off_arms_live():
    assert "arms LIVE" not in HELP and "one-time Go-live button" in HELP and "/dry off confirm" in HELP
    assert "(practice and live)" in HELP and "dry and live" not in HELP
    st = engine_mod.SafetyState(live=False, locked=False, live_until=None, panic=False, paused=False, spent_24h=0,
                                buys_last_hour=0, max_price=10, daily_budget=50, max_per_hour=3)
    text = safety_text(st, settings(**LIVE))
    assert "arms LIVE" not in text and "shows the practice report" in text


# --- low: the near-miss card --------------------------------------------------------------------------------


def test_near_miss_card_has_why_risk_and_a_why_button():
    deal = assess(L("n", 2.62), FloorEstimate(3.2, "cluster", 6, "high"), FeeModel(), [])
    card = render_near_miss(L("n", 2.62), deal, BuyRule("X", 2.5, id=3), NearMiss("max", 2.62, 2.5, 4.8, 2.65),
                            "tok12345", max_cap=10, practice=True)
    assert "<blockquote expandable>Why:" in card.html and "It misses rule #3" in card.html
    assert "Risk:" in card.plain
    assert "Why?" in keyboard_labels(card.keyboard)


def test_shadow_rule_near_miss_is_practice_inside_a_live_window():
    async def go():
        rules = [replace(r, state="shadow") if r.collection == "Desk Calendar" else r for r in DEMO_RULES]
        engine, storage, client, notifier, _ = await demo_engine(settings(**LIVE), rules=rules, force_dry=False)
        try:
            await engine.arm_live()
            await engine.run_cycle()
            [near] = [m for m in notifier.messages if m.startswith("NEAR MISS")]
            assert near.startswith("NEAR MISS PRACTICE · shadow rule · Desk Calendar #3001")
        finally:
            await storage.close()

    asyncio.run(go())


def test_near_miss_why_button_answers():
    async def go():
        engine, storage, *_ = await demo_engine()
        try:
            await engine.run_cycle()
            [card] = [c for c in await storage.list_pending(("near",))]
            res = await engine.tap("w", card.token, ADMIN)
            assert res.ok and res.reply and "Desk Calendar" in res.reply
        finally:
            await storage.close()

    asyncio.run(go())


# --- low: the ask-first threshold follows a /preset ---------------------------------------------------------


def test_ask_first_threshold_follows_a_preset_max():
    async def go():
        engine, storage, client, notifier, _ = await demo_engine()
        try:
            await storage.set_setting("limit:MAX_PRICE_TON", 5)  # /preset careful with MAX_PRICE_TON=10 in .env
            text = safety_text(await engine.safety_state(), engine.settings)
            assert "One gift: max 5 TON" in text and "Buys above 2.5 TON always ask first." in text
            report = await engine.run_cycle()
            assert report.reasons["CONFIRM_ABOVE"] >= 1  # hc-1234 at 3.00 TON (auto rule) asks first now
            assert not [p for p in await storage.recent_purchases() if p.nft_id == "hc-1234"]
        finally:
            await storage.close()

    asyncio.run(go())


def test_explicit_confirm_above_is_kept():
    s = settings(CONFIRM_ABOVE_TON=4)
    assert s.confirm_above(5) == 4 and s.confirm_above_ton == 4
    assert settings().confirm_above(5) == 2.5


# --- low: scaffold leftovers are gone, and every reason text is reachable -----------------------------------


def test_scaffold_leftovers_are_removed():
    for mod, name in ((engine_mod, "comparable_floor"), (engine_mod, "check_limits"), (engine_mod, "reason_label"),
                      (notifier_mod, "format_alert"), (notifier_mod, "format_buy"), (notifier_mod, "format_listing"),
                      (rules_mod, "rule_from_dict"), (bot_mod, "COMMANDS"), (client_mod, "mock_fixtures"),
                      (client_mod, "mock_rules")):
        assert not hasattr(mod, name), name
    for prop in ("cycles", "last_cycle_at", "last_error", "consecutive_errors"):
        assert not hasattr(engine_mod.Engine, prop), prop
    assert not hasattr(MockClient(), "scenario")


def test_every_reason_text_is_emitted_somewhere():
    pkg = ROOT / "portals_autobuy"
    source = "\n".join((pkg / n).read_text() for n in ("engine.py", "peers.py", "pricing.py", "rules.py", "bot.py"))
    reachable = set(explain.SUITE_CODE.values()) | {"OK"}
    dead = sorted(c for c in explain.REASON_TEXT if c not in reachable and f'"{c}"' not in source)
    assert dead == []


# --- low: /unmute takes the same names as /mute -----------------------------------------------------------


def test_unmute_accepts_what_mute_accepts():
    assert mute_key("Lunar Snake:Albino") == mute_key("lunar snake:albino") == "lunar snake:albino"

    async def go():
        engine, storage, client, _, _ = await demo_engine()
        b = Bot(engine, storage, client)
        try:
            await b.send("mute", "Lunar Snake:Albino 2")
            assert (await b.send("unmute", "Lunar Snake:Albino"))[0][0] == "Unmuted lunar snake:albino."
        finally:
            await storage.close()

    asyncio.run(go())



# --- low: local secret files stay out of any packaged copy ----------------------------------------------------


def test_gitignore_covers_local_env_files():
    lines = {line.strip() for line in (ROOT / ".gitignore").read_text().splitlines()}
    assert {".env", ".env.*", "data/", "*.session*", "rules.json"} <= lines
