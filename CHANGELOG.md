# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-10-28

First public release.

### Added

- Sniping loop for Portals: rules by collection, model, backdrop and symbol. Each cycle fetches the newest listings across the rule collections and the cheapest listings per rule filter, deduplicated by listing id and price.
- Honest price math (`pricing.py`): a cluster floor with `high`/`med`/`low` confidence (a floor without a cluster of at least 3 listings is `low`), a fee model (buy 0%, sell 5%, withdraw 0.1 TON by default, and 0% sell fee for collections Portals marks `zero_commission`), the all-in cost, the net exit at the floor, the break-even price and match reason codes.
- Trap and lock filter: bundles (`bundle_id`) are blocked and excluded from search, locked gifts (`unlocks_at`) are skipped unless a rule allows the lock, unknown price units are refused, and prices under `TOO_GOOD_PCT` of the floor always ask first.
- Deal cards in Telegram HTML with per-rule `auto`, `confirm` and `watch` modes, Buy / Skip / Mute 24h / Why? buttons, trait rarity, a price re-check before a tapped buy, a 90 s expiry and near-miss cards with a one-tap "Raise max".
- Second opinion (`peers.py`, `fx.py`): Telegram's own gift valuation looked up by the listing's `tg_id`, MRKT and Tonnel floor snapshot files, and a keyless TON to USD rate. Adds `/peers`, `/compare` and `floors --json`.
- Practice ledger (`ledger.py`): positions marked net of fees, `/pnl`, `/score`, `/scoreboard`, `/sold` and `/export`.
- `/hunt` wizard (`wizard.py`): a rule in five steps with buttons, `t.me/nft` link support and a "would have caught" preview of the last 24 h, with model floors from the public filters endpoint.
- `/doctor` in the bot and as a CLI command (exit code 1 on any failure). It checks the market first, so the clock check can read the server's `Date` header.
- Explained decisions: a `decisions` table, `/why`, a JSON-lines event log, an optional daily digest with tuning hints and spend against budget, `/mute` and `/unmute`, and an optional "Explain more" button (needs `ANTHROPIC_API_KEY`).
- Shadow rules with a hindsight card, plus `/promote`.
- `/buys` tags each row as `PRACTICE bought`, `PRACTICE not bought`, `LIVE bought`, `LIVE failed` or `LIVE result unknown`.
- `/start` onboarding: one paragraph on what the bot does, the mode and limits, and three commands to try first (`/hunt`, `/scoreboard`, `/doctor`). `/help` lists every command.
- `TZ_OFFSET_HOURS` defaults to `0` (UTC), and every time the bot shows names its zone (`UTC`, `UTC+3`): `/why`, `/buys`, `/mute`, `/safety` and `/resume`.
- Near-miss cards have the deal-card layout: a Risk line, an expandable Why section and a **Why?** button.
- `mock` command that runs the whole pipeline offline with fixtures, with `--tap`, `--crash`, `--panic`, `--preview`, `--doctor` and `--shadow-hours`.
- Windows install (README: Self-hosting), a CI workflow (tests on Python 3.11 and 3.12) and 277 offline tests.

### Safety

- `DRY_RUN=true` in `.env` is a hard practice lock that no bot command or stored setting (`/dry off` or a stored `dry` flag) can lift.
- Live mode needs `DRY_RUN=false` and `/dry off`. It shows the practice report first, uses a one-time go-live button that expires after 10 minutes and only appears when every enabled rule is `ready`, and switches back to practice after `LIVE_ARM_HOURS`. The bot fails closed: without an armed gate whose `live_until` is valid (readable, finite, no further ahead than `LIVE_ARM_HOURS`) it is in practice, so a fresh database with `DRY_RUN=false` still starts in practice.
- Every buy, automatic or tapped, practice or live, goes through one locked spend step that re-reads every switch and the rule, and checks `MAX_PRICE_TON`, `DAILY_BUDGET_TON` and `MAX_BUYS_PER_HOUR` against fresh totals. Live buys re-check the switches once more right before `POST /nfts`.
- A live buy is recorded as `unknown` before the order goes out, so a buy cut off by a timeout, an error or a shutdown still counts toward the limits. Only an explicit success turns it into a buy: any other answer (an unexpected 2xx body, a timeout, a 5xx) stays `unknown` and counts toward `DAILY_BUDGET_TON` and `MAX_BUYS_PER_HOUR`. On stop, the bot refuses new buys and waits for one in flight to finish.
- Arming live mode expires every pending practice card, and a tapped card keeps its own practice flag (a card without one counts as practice), so a card sent in practice only ever makes a practice buy. Buy and "Raise max" buttons expire and only answer `ADMIN_IDS`. A rule edited or switched out of `auto` during a buy stops that buy.
- A floor not backed by a cluster of at least 3 listings is rated `low`, so two decoy listings can never set a `med` floor that passes the default `min_confidence=med`.
- Limits fail closed. `.env` refuses `nan` and `inf` for every number, `/addrule` and `rules.json` refuse non-finite numbers, and `rules.json` also rejects a max price of 0 or below. A limit check that meets a NaN blocks the buy.
- `/panic` pauses, forces practice, voids the go-live button and expires pending cards. `/resume` needs a confirmation.
- Resume buttons are one-time tokens, like the go-live button. The **Confirm resume** button of `/resume` and the **Resume** button of the balance breaker work once, for 10 minutes, and any newer stop (`/pause`, `/panic`, a breaker) voids them, so an old button cannot undo a newer `/panic`. A floor-crash card's **Resume** works only for that rule's newest pause.
- `/resume` says which mode buying resumes in. After `/panic` it is practice until a new `/dry off`; after `/pause` or a breaker inside an open live window it says LIVE (real buys continue), and the balance-breaker card warns about it before you tap.
- The ask-first threshold (`CONFIRM_ABOVE_TON=0`, half of the max price) follows a `/preset` that lowers the max.
- Breakers: floor crash per rule, a losing streak, a balance mismatch (available plus Portals frozen funds) and repeated failed buys. `/pause`, `/panic` and every breaker are re-read right before the buy request. Card expiry, the end of the live window and the breakers keep running while cycles fail.
- `/safety`, plus `/preset` limits that never go above `.env`.
- New rules, including new `rules.json` entries without a `state`, start in shadow (practice only) until promoted. A promotion survives a restart.
- Secrets are truncated in logs, the session file is set to mode 600, and `/doctor` checks its permissions and 2FA.

### Known limitations

- The live paths have not run against Portals with a real account, and the admin bot has not run with a real bot token. Everything marked `TODO(verify)` in the code is listed in the README under "Before going live": the buy response, fee schedule, whether the fee is on top of the price, the balance response and frozen funds, the Mini App short name, the `initData` lifetime, rate limits with auth, newest-first ordering, premarket and locked gifts, the meaning of `unlocks_at`, price units, the single-gift re-check, collections pagination, backdrop floors and Telegram's "same type" definition.
- Fee defaults are assumptions until you set `FEES_VERIFIED=true`.
- The success body of the Portals buy request is unverified. Until it is checked, a reply in an unexpected shape is reported as `LIVE result unknown` and counted toward the limits; check it in Portals.
- A buy already sent to Portals cannot be recalled by `/panic`.
- Live buys run one at a time because of the spend lock.
- Practice assumes you won every race for the listing.
- File rules reset their `mode` and `max` from `rules.json` on each start, and deleting `rules.json` deletes the rules it loaded.
- `doctor` reports the Telegram session as failed until you run `login`, even in practice mode.
