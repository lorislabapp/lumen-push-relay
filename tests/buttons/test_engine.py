"""Tests for ButtonEngine — match → cooldown gate → VoIP dispatch."""
import asyncio
import pytest

from src.auto_call.buttons.engine import ButtonEngine
from src.auto_call.buttons.models import MatchRule, PhysicalButtonBinding


class FakeDispatcher:
    def __init__(self): self.calls: list[dict] = []
    async def dispatch(self, **kw):
        self.calls.append(kw)
        return True


class FakeCooldown:
    def __init__(self): self.hot: set[tuple[str, str]] = set()
    def is_hot(self, token, camera, _seconds): return (token, camera) in self.hot
    def mark_hot(self, token, camera): self.hot.add((token, camera))


class FakeConfigStore:
    """Minimal stand-in. The real ConfigStore exposes
    bindings_matching_topic + camera_cooldown_for(token, camera_id)."""
    def __init__(self, mapping):
        # mapping: list[(token_str, PhysicalButtonBinding)]
        self.mapping = mapping
    def bindings_matching_topic(self, topic):
        return [(token, b) for token, b in self.mapping if b.topic == topic]
    def camera_cooldown_for(self, token, camera_id):
        return 60


def _binding(topic="test/btn", cameras=None, enabled=True,
             pattern_type="any", pattern_value=None, path=None):
    return PhysicalButtonBinding(
        id="00000000-0000-0000-0000-000000000001",
        name="B", topic=topic,
        match=MatchRule(path=path, pattern_type=pattern_type, pattern_value=pattern_value),
        camera_ids=list(cameras or ["front"]), enabled=enabled,
    )


@pytest.mark.asyncio
async def test_dispatches_for_matching_binding():
    cs = FakeConfigStore([("tok1", _binding(cameras=["front"]))])
    disp, cd = FakeDispatcher(), FakeCooldown()
    eng = ButtonEngine(config_store=cs, dispatcher=disp, cooldown=cd)
    n = await eng.handle_message("test/btn", b"anything")
    assert n == 1
    assert disp.calls[0]["camera_id"] == "front"
    assert disp.calls[0]["device_token_hex"] == "tok1"
    assert disp.calls[0]["trigger_type"] == "physicalButton"


@pytest.mark.asyncio
async def test_dispatches_to_all_camera_ids_in_binding():
    cs = FakeConfigStore([("tok1", _binding(cameras=["front", "porch"]))])
    disp, cd = FakeDispatcher(), FakeCooldown()
    eng = ButtonEngine(config_store=cs, dispatcher=disp, cooldown=cd)
    n = await eng.handle_message("test/btn", b"")
    assert n == 2
    assert {c["camera_id"] for c in disp.calls} == {"front", "porch"}


@pytest.mark.asyncio
async def test_skips_disabled_binding():
    cs = FakeConfigStore([("tok1", _binding(enabled=False))])
    disp, cd = FakeDispatcher(), FakeCooldown()
    eng = ButtonEngine(config_store=cs, dispatcher=disp, cooldown=cd)
    n = await eng.handle_message("test/btn", b"")
    assert n == 0
    assert disp.calls == []


@pytest.mark.asyncio
async def test_skips_when_match_fails():
    cs = FakeConfigStore([("tok1", _binding(pattern_type="equals", pattern_value="ring"))])
    disp, cd = FakeDispatcher(), FakeCooldown()
    eng = ButtonEngine(config_store=cs, dispatcher=disp, cooldown=cd)
    n = await eng.handle_message("test/btn", b"different")
    assert n == 0


@pytest.mark.asyncio
async def test_cooldown_blocks_dispatch():
    cs = FakeConfigStore([("tok1", _binding(cameras=["front"]))])
    disp, cd = FakeDispatcher(), FakeCooldown()
    cd.mark_hot("tok1", "front")
    eng = ButtonEngine(config_store=cs, dispatcher=disp, cooldown=cd)
    n = await eng.handle_message("test/btn", b"")
    assert n == 0


@pytest.mark.asyncio
async def test_marks_cooldown_only_on_dispatch_success():
    cs = FakeConfigStore([("tok1", _binding(cameras=["front"]))])
    disp, cd = FakeDispatcher(), FakeCooldown()
    eng = ButtonEngine(config_store=cs, dispatcher=disp, cooldown=cd)
    await eng.handle_message("test/btn", b"")
    assert ("tok1", "front") in cd.hot


@pytest.mark.asyncio
async def test_multiple_users_bound_to_same_topic():
    cs = FakeConfigStore([
        ("tok1", _binding(cameras=["front"])),
        ("tok2", _binding(cameras=["front"])),
    ])
    disp, cd = FakeDispatcher(), FakeCooldown()
    eng = ButtonEngine(config_store=cs, dispatcher=disp, cooldown=cd)
    n = await eng.handle_message("test/btn", b"")
    assert n == 2
    assert {c["device_token_hex"] for c in disp.calls} == {"tok1", "tok2"}


class FlakyDispatcher:
    """First call raises, second call returns True. Used to validate that
    one binding's dispatch failure does not abort the fan-out loop."""
    def __init__(self):
        self.calls: list[dict] = []
    async def dispatch(self, **kw):
        self.calls.append(kw)
        if len(self.calls) == 1:
            raise RuntimeError("simulated APNs blip")
        return True


@pytest.mark.asyncio
async def test_dispatcher_exception_does_not_halt_other_users():
    cs = FakeConfigStore([
        ("tok1", _binding(cameras=["front"])),
        ("tok2", _binding(cameras=["front"])),
    ])
    disp, cd = FlakyDispatcher(), FakeCooldown()
    eng = ButtonEngine(config_store=cs, dispatcher=disp, cooldown=cd)
    n = await eng.handle_message("test/btn", b"")
    # First call raised → not counted; second call succeeded → counted.
    assert n == 1
    # Both dispatch attempts ran (proves the exception was isolated to the failing call).
    assert len(disp.calls) == 2
    # Cooldown only marked hot for the user whose dispatch succeeded.
    assert ("tok2", "front") in cd.hot
    assert ("tok1", "front") not in cd.hot
