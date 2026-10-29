"""Portals client additions: Date header (clock skew for /doctor), frozen funds, request counting in mock."""

import asyncio

import pytest
from helpers import Clock, demo_engine, settings

from portals_autobuy.client import MockClient, PortalsClient


class FakeHttp:
    kind = "fake"

    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    async def request(self, method, url, headers, body=None):
        self.urls.append(url)
        return self.responses.pop(0)

    async def close(self):
        return None


def test_date_header_and_frozen_funds_are_recorded():
    async def go():
        s = settings(PORTALS_INIT_DATA="query_id=AAE&hash=x", REQUESTS_PER_SECOND=100)
        client = PortalsClient(s)
        await client._http.close()
        client._http = FakeHttp([
            (200, {"date": "Wed, 28 Oct 2026 18:47:34 GMT"}, '{"collections": []}'),
            (200, {}, '{"balance": "12.5", "frozen_funds": "2"}'),
        ])
        assert await client.collections() == []
        assert client.last_server_date == pytest.approx(1793213254.0)
        assert client.last_latency_ms is not None and client.last_response_at is not None
        assert await client.balance() == 12.5 and client.last_frozen == 2.0
        assert client._http.urls[1].endswith("/users/wallets/")
        await client.close()

    asyncio.run(go())


def test_cycle_makes_no_extra_requests_compared_to_the_scaffold():
    """Scaffold per cycle: /collections (cached FLOORS_TTL), one newest search, one cheapest search per rule and
    /collections/filters for pinned attributes (cached). Peers, Telegram value and snapshots add no market requests."""

    async def go():
        clock = Clock()
        engine, storage, client, notifier, _ = await demo_engine(clock=clock, snapshot_path=None)
        try:
            await engine.run_cycle()
            assert client.requests == 1 + 1 + 6 + 1
            before = client.requests
            client.advance()
            clock.advance(5)
            await engine.run_cycle()
            assert client.requests - before == 1 + 6  # floors and filters still cached
            assert all(c.startswith("search:") for c in client.calls)  # practice never calls buy()
        finally:
            await storage.close()

    asyncio.run(go())
