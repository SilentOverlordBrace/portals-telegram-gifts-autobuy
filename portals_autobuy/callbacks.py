"""Inline-button callback data. Every packed value stays well under Telegram's 64-byte limit."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class DealCB(CallbackData, prefix="d"):
    """Deal card: b=buy, s=skip, m=mute 24h, w=why, x=explain more (LLM). token = 8 chars."""

    action: str
    token: str


class NearCB(CallbackData, prefix="n"):
    """Near-miss card: r=raise max, i=ignore today."""

    action: str
    token: str


class RuleCB(CallbackData, prefix="r"):
    """Floor-crash card of one rule: keep (stay paused). 'resume' is legacy: see RuleResumeCB."""

    action: str
    rule_id: int


class RuleResumeCB(CallbackData, prefix="rr"):
    """[Resume] on a floor-crash card: a one-time nonce that the rule's next crash pause replaces."""

    rule_id: int
    nonce: str


class GateCB(CallbackData, prefix="g"):
    """Buttons that change nothing: stay (in practice) | keep (stay paused). 'live' and 'resume' are legacy:
    going live is LiveCB, resuming ResumeCB."""

    action: str


class ResumeCB(CallbackData, prefix="rs"):
    """[Confirm resume] after /resume, or [Resume] after the balance breaker: a one-time nonce that the next
    stop (/pause, /panic, a breaker) voids."""

    nonce: str


class LiveCB(CallbackData, prefix="gl"):
    """[Go live for 24 h] from a /dry off report: a one-time nonce that /panic and /dry on void."""

    nonce: str


class ShadowCB(CallbackData, prefix="sh"):
    """Hindsight card of a shadow rule: live | more (keep testing 24 h) | del."""

    action: str
    rule_id: int


class PresetCB(CallbackData, prefix="ps"):
    """/preset confirmation: ok | no."""

    action: str
    name: str
