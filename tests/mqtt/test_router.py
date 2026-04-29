import pytest

from src.auto_call.mqtt.router import SubscriptionManager, TopicRouter


class FakeMQTT:
    def __init__(self):
        self.subbed: list[str] = []
        self.unsubbed: list[str] = []
    async def subscribe(self, topic): self.subbed.append(topic)
    async def unsubscribe(self, topic): self.unsubbed.append(topic)


# --- SubscriptionManager ---

@pytest.mark.asyncio
async def test_subscription_reconcile_adds_only_new():
    m = FakeMQTT()
    s = SubscriptionManager(m)
    await s.reconcile({"a", "b"})
    assert sorted(m.subbed) == ["a", "b"]
    assert m.unsubbed == []
    m.subbed.clear()
    await s.reconcile({"a", "b", "c"})
    assert m.subbed == ["c"]


@pytest.mark.asyncio
async def test_subscription_reconcile_removes_stale():
    m = FakeMQTT()
    s = SubscriptionManager(m)
    await s.reconcile({"a", "b", "c"})
    m.subbed.clear()
    await s.reconcile({"a"})
    assert sorted(m.unsubbed) == ["b", "c"]


@pytest.mark.asyncio
async def test_subscription_reconcile_idempotent_on_identical_set():
    m = FakeMQTT()
    s = SubscriptionManager(m)
    await s.reconcile({"a"})
    m.subbed.clear()
    await s.reconcile({"a"})
    assert m.subbed == []
    assert m.unsubbed == []


# --- TopicRouter ---

@pytest.mark.asyncio
async def test_router_dispatches_frigate_topic():
    calls = []
    async def frigate(topic, payload): calls.append(("frigate", topic))
    cache = type("C", (), {"update": lambda self, t, p: calls.append(("cache", t))})()
    button_engine = type("B", (), {"handle_message": lambda self, t, p: calls.append(("button", t))})()

    r = TopicRouter(
        frigate_topic="frigate/events",
        ha_prefix="homeassistant",
        frigate_handler=frigate,
        discovery_cache=cache,
        button_engine=button_engine,
    )
    await r.route("frigate/events", b"{}")
    assert calls == [("frigate", "frigate/events")]


@pytest.mark.asyncio
async def test_router_dispatches_ha_discovery_to_cache():
    calls = []
    async def frigate(topic, payload): calls.append(("frigate", topic))
    class C:
        def update(self, t, p): calls.append(("cache", t))
    class B:
        async def handle_message(self, t, p): calls.append(("button", t))

    r = TopicRouter("frigate/events", "homeassistant", frigate, C(), B())
    await r.route("homeassistant/button/x/config", b"{}")
    assert calls == [("cache", "homeassistant/button/x/config")]


@pytest.mark.asyncio
async def test_router_dispatches_other_topic_to_button_engine():
    calls = []
    async def frigate(topic, payload): calls.append(("frigate", topic))
    class C:
        def update(self, t, p): calls.append(("cache", t))
    class B:
        async def handle_message(self, t, p): calls.append(("button", t))

    r = TopicRouter("frigate/events", "homeassistant", frigate, C(), B())
    await r.route("zigbee2mqtt/aqara/action", b"")
    assert calls == [("button", "zigbee2mqtt/aqara/action")]


@pytest.mark.asyncio
async def test_router_handler_exception_does_not_propagate():
    async def frigate(topic, payload): raise RuntimeError("boom")
    class C:
        def update(self, t, p): pass
    class B:
        async def handle_message(self, t, p): pass

    r = TopicRouter("frigate/events", "homeassistant", frigate, C(), B())
    # Must not raise.
    await r.route("frigate/events", b"")
