"""Regressions for lumen-push-relay 1.2.2."""
import asyncio
import json
import logging
from types import SimpleNamespace

import httpx
import pytest

import relay
from src.review_contract import ReviewCorrelator

T0 = 1_793_156_169.328867  # Front Door Review 1793156169.328867-h7imo6
CONFIG = {'filters': {'labels': ['person', 'car'], 'min_score': .80, 'max_event_age_seconds': 120,
                      'review_severities': ['alert']}}


def ev(kind='new', score=.779, at=T0, label='person', ended=False, eid='ev1', stationary=False, start=T0):
    after = {'id': eid, 'camera': 'front_door', 'label': label, 'top_score': score,
             'start_time': start, 'frame_time': at, 'stationary': stationary, 'zones': []}
    if ended:
        after['end_time'] = at
    return {'type': kind, 'after': after}


def rv(kind='new', severity='alert', end=None, ids=('ev1',)):
    after = {'id': '1793156169.328867-h7imo6', 'camera': 'front_door', 'severity': severity,
             'start_time': T0, 'end_time': end, 'data': {'detections': list(ids)}}
    return {'type': kind, 'after': after}


@pytest.fixture
def clock(monkeypatch):
    now = {'t': T0}
    monkeypatch.setattr(relay.time, 'time', lambda: now['t'])
    return now


@pytest.fixture
def correlator(clock):
    return ReviewCorrelator(
        event_rejection=lambda e: relay.suppression_code(relay.should_notify(e, CONFIG)),
        allowed_severities=frozenset(['alert']))


def at(clock, t):
    clock['t'] = t
    return t


# --- 1. "end" messages and open Reviews -------------------------------------

def test_front_door_final_score_on_event_end_is_sent_once(correlator, clock):
    assert correlator.ingest_event_message(ev(score=.779, at=T0 + 1), now=at(clock, T0 + 1)) == []
    assert correlator.ingest_review_message(rv(), now=at(clock, T0 + 2)) == []
    # 1.2.1 dropped this message (type "end", and > 120 s after start).
    out = correlator.ingest_event_message(ev('end', .844, T0 + 140, ended=True), now=at(clock, T0 + 141))
    assert len(out) == 1 and out[0]['score'] == .844
    correlator.complete_delivery(out[0], terminal=True, now=T0 + 141)
    # Review "end" afterwards must never resend.
    assert correlator.ingest_review_message(rv('end', end=T0 + 150), now=at(clock, T0 + 151)) == []
    assert correlator.ingest_event_message(ev('end', .9, T0 + 152, ended=True), now=at(clock, T0 + 152)) == []


def test_open_review_stays_eligible_after_120s(correlator, clock):
    correlator.ingest_event_message(ev(score=.779, at=T0 + 1), now=at(clock, T0 + 1))
    correlator.ingest_review_message(rv(), now=at(clock, T0 + 2))
    out = correlator.ingest_event_message(ev('update', .822, T0 + 300), now=at(clock, T0 + 300))
    assert len(out) == 1 and out[0]['score'] == .822


def test_review_end_with_final_score_resolves(correlator, clock):
    correlator.ingest_review_message(rv(), now=at(clock, T0 + 2))
    correlator.ingest_event_message(ev(score=.779, at=T0 + 3), now=at(clock, T0 + 3))
    # Event update arrives (score crosses) but the correlator only learns via the Review end.
    correlator._events['ev1']['top_score'] = .844
    correlator._events['ev1']['frame_time'] = T0 + 200
    assert len(correlator.ingest_review_message(rv('end', end=T0 + 200), now=at(clock, T0 + 201))) == 1


def test_hard_cap_fifteen_minutes(correlator, clock, caplog):
    caplog.set_level(logging.INFO, logger='frigate-push')
    correlator.ingest_event_message(ev(score=.779, at=T0 + 1), now=at(clock, T0 + 1))
    correlator.ingest_review_message(rv(), now=at(clock, T0 + 2))
    assert correlator.ingest_event_message(ev('update', .95, T0 + 901), now=at(clock, T0 + 901)) == []
    assert 'not sent: expired' in caplog.text


def test_stale_event_without_fresh_activity_is_still_rejected(correlator, clock):
    stale = ev(score=.95, at=T0)
    assert correlator.ingest_event_message(stale, now=at(clock, T0 + 121)) == []


def test_delivered_review_end_is_not_requeued(correlator, clock, caplog):
    caplog.set_level(logging.INFO, logger='frigate-push')
    correlator.ingest_event_message(ev(score=.9, at=T0 + 1), now=at(clock, T0 + 1))
    out = correlator.ingest_review_message(rv(), now=at(clock, T0 + 2))
    correlator.complete_delivery(out[0], terminal=True, now=T0 + 2)
    assert correlator.ingest_review_message(rv('end', end=T0 + 30), now=at(clock, T0 + 31)) == []
    assert not correlator._pending
    correlator.ingest_event_message(ev('update', .9, T0 + 100), now=at(clock, T0 + 100))
    assert 'not sent' not in caplog.text


@pytest.mark.parametrize('kwargs,reason', [
    ({'score': .779}, 'below_min_score'),
    ({'score': .95, 'label': 'dog'}, 'label_filtered'),
    ({'score': .95, 'stationary': True}, 'stationary'),
])
def test_not_sent_reason_logged_when_review_and_events_end(correlator, clock, caplog, kwargs, reason):
    caplog.set_level(logging.INFO, logger='frigate-push')
    correlator.ingest_event_message(ev(at=T0 + 1, **kwargs), now=at(clock, T0 + 1))
    correlator.ingest_review_message(rv(), now=at(clock, T0 + 2))
    correlator.ingest_event_message(ev('end', at=T0 + 40, ended=True, **kwargs), now=at(clock, T0 + 40))
    assert correlator.ingest_review_message(rv('end', end=T0 + 45), now=at(clock, T0 + 45)) == []
    assert not correlator._pending
    assert f'Review 1793156169.328867-h7imo6 not sent: {reason}' in caplog.text


def test_not_sent_reason_severity_after_grace(correlator, clock, caplog):
    caplog.set_level(logging.INFO, logger='frigate-push')
    correlator.ingest_event_message(ev(score=.95, at=T0 + 1), now=at(clock, T0 + 1))
    correlator.ingest_review_message(rv(severity='detection'), now=at(clock, T0 + 2))
    correlator.ingest_review_message(rv('end', severity='detection', end=T0 + 20), now=at(clock, T0 + 20))
    correlator.ingest_event_message(ev('update', .95, T0 + 60, eid='other'), now=at(clock, T0 + 60))
    assert 'not sent: severity' in caplog.text


def test_review_end_waits_for_straggling_event_end(correlator, clock):
    correlator.ingest_event_message(ev(score=.779, at=T0 + 1), now=at(clock, T0 + 1))
    correlator.ingest_review_message(rv(), now=at(clock, T0 + 2))
    assert correlator.ingest_review_message(rv('end', end=T0 + 130), now=at(clock, T0 + 130)) == []
    out = correlator.ingest_event_message(ev('end', .844, T0 + 135, ended=True), now=at(clock, T0 + 136))
    assert len(out) == 1


# --- MQTT loop harness ----------------------------------------------------

async def run_loop(monkeypatch, tmp_path, steps, clock, results=None, resolver=None, config_extra=None):
    sent = []
    outcomes = iter(results or [])

    class Sender:
        async def send(self, envelope):
            sent.append(envelope)
            return next(outcomes, True)

        async def close(self):
            pass

    class Client:
        def __init__(self, **options):
            self.messages = self.stream()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def subscribe(self, topic):
            pass

        async def stream(self):
            for t, topic, value in steps:
                clock['t'] = t
                yield SimpleNamespace(topic=topic, payload=json.dumps(value).encode())
            raise asyncio.CancelledError()

    config = {'mode': 'worker', 'worker': {'push_urls': ['https://push.example.invalid/owned']},
              'frigate': {'mqtt_host': 'broker.invalid', 'mqtt_port': 1883, 'topic': 'frigate/reviews'},
              'filters': {**CONFIG['filters'], 'cooldown_seconds': 120}}
    if config_extra:
        config['frigate'].update(config_extra)
    monkeypatch.setattr(relay, 'load_config', lambda: config)
    monkeypatch.setattr(relay, 'WorkerSender', lambda urls: Sender())
    monkeypatch.setattr(relay.aiomqtt, 'Client', Client)
    if resolver is not None:
        monkeypatch.setattr(relay, 'camera_resolver_from_config', lambda cfg: resolver)
    monkeypatch.setenv('LUMEN_REVIEW_STATE_PATH', str(tmp_path / 'state.json'))
    with pytest.raises(asyncio.CancelledError):
        await relay.run()
    return sent


def incident_steps():
    return [
        (T0 + 1, 'frigate/events', ev(score=.779, at=T0 + 1)),
        (T0 + 2, 'frigate/reviews', rv()),
        (T0 + 140, 'frigate/events', ev('end', .844, T0 + 140, ended=True)),
        (T0 + 150, 'frigate/reviews', rv('end', end=T0 + 150)),
    ]


@pytest.mark.asyncio
async def test_mqtt_front_door_incident_replay_sends_once_and_records_state(monkeypatch, tmp_path, clock):
    sent = await run_loop(monkeypatch, tmp_path, incident_steps(), clock)
    assert len(sent) == 1 and sent[0]['score'] == .844
    state = json.loads((tmp_path / 'state.json').read_text())
    assert '1793156169.328867-h7imo6/alert' in state['delivered']


# --- 2. Worker JSON response ----------------------------------------------

def worker(responses, sleeps):
    calls = iter(responses)

    def handler(request):
        status, body = next(calls)
        return httpx.Response(status, text=body)

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return relay.WorkerSender(['https://push.example.invalid/v2/notify/aaaa/bbbbbbbbbb'], client=client, sleep=fake_sleep)


def body(delivered, *outcomes):
    return json.dumps({'ok': True, 'delivered': delivered, 'suppressed': len(outcomes) - delivered,
                       'outcomes': [{'outcome': o, 'reason': 'r'} for o in outcomes]})


@pytest.mark.asyncio
@pytest.mark.parametrize('responses,accepted,notified,n_sleeps', [
    ([(200, 'OK')], True, True, 0),
    ([(200, body(1, 'sent'))], True, True, 0),
    ([(200, body(0, 'rate_limited')), (200, body(1, 'sent'))], True, True, 1),
    ([(200, body(0, 'rate_limited')), (200, body(0, 'rate_limited'))], False, False, 1),
    ([(200, body(0, 'duplicate'))], True, False, 0),
    ([(200, body(0, 'filtered', 'cooldown'))], True, False, 0),
    ([(200, body(0, 'failed'))], False, False, 0),
    ([(500, 'boom')], False, False, 0),
    ([(200, json.dumps({'ok': False}))], False, False, 0),
])
async def test_worker_response_contract(responses, accepted, notified, n_sleeps):
    sleeps = []
    sender = worker(responses, sleeps)
    result = await sender.send({'reviewId': 'r', 'reviewSeverity': 'alert'})
    await sender.close()
    assert (result.accepted, result.notified) == (accepted, notified)
    assert sleeps == [6.0] * n_sleeps


@pytest.mark.asyncio
async def test_worker_suppression_records_state_and_releases_cooldown(monkeypatch, tmp_path, clock):
    suppressed = relay.DeliveryResult(accepted=True, notified=False)
    steps = incident_steps() + [
        (T0 + 160, 'frigate/events', ev('new', .95, T0 + 160, eid='ev2', start=T0 + 160)),
        (T0 + 161, 'frigate/reviews', {'type': 'new', 'after': {
            'id': 'second', 'camera': 'front_door', 'severity': 'alert', 'start_time': T0 + 160,
            'data': {'detections': ['ev2']}}}),
    ]
    sent = await run_loop(monkeypatch, tmp_path, steps, clock, results=[suppressed, True])
    # Suppressed by Worker → terminal (state recorded) and local cooldown released,
    # so the second Review 21 s later is not blocked by the 120 s cooldown.
    assert [e['reviewId'] for e in sent] == ['1793156169.328867-h7imo6', 'second']
    state = json.loads((tmp_path / 'state.json').read_text())
    assert {'1793156169.328867-h7imo6/alert', 'second/alert'} <= set(state['delivered'])


@pytest.mark.asyncio
async def test_worker_rate_limited_after_retry_stays_retryable(monkeypatch, tmp_path, clock):
    failed = relay.DeliveryResult(accepted=False, notified=False)
    steps = incident_steps()[:3] + [(T0 + 150, 'frigate/reviews', rv('update'))]
    sent = await run_loop(monkeypatch, tmp_path, steps, clock, results=[failed, True])
    assert len(sent) == 2


# --- 3. REVIEW_SEVERITIES alias -------------------------------------------

def env_config(monkeypatch, **env):
    for key in ('REVIEW_SEVERITIES', 'FILTER_REVIEW_SEVERITIES', 'PUSH_URL', 'PUSH_URLS', 'FRIGATE_URL'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('CONFIG_PATH', '/nonexistent/config.yaml')
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return relay.load_config()


def test_filter_review_severities_is_deprecated_alias(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger='frigate-push')
    cfg = env_config(monkeypatch, FILTER_REVIEW_SEVERITIES='alert,detection')
    assert cfg['filters']['review_severities'] == ['alert', 'detection']
    assert 'deprecated' in caplog.text


def test_review_severities_wins_over_alias(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger='frigate-push')
    cfg = env_config(monkeypatch, REVIEW_SEVERITIES='alert', FILTER_REVIEW_SEVERITIES='detection')
    assert cfg['filters']['review_severities'] == ['alert']
    assert 'ignored' in caplog.text


def test_default_severity_and_no_frigate_url(monkeypatch):
    cfg = env_config(monkeypatch)
    assert cfg['filters']['review_severities'] == ['alert']
    assert 'url' not in cfg['frigate']
    assert relay.camera_resolver_from_config(cfg['frigate']) is None


def test_frigate_url_env_is_opt_in(monkeypatch):
    cfg = env_config(monkeypatch, FRIGATE_URL='http://frigate.invalid:5000', FRIGATE_TOKEN='t0k')
    assert cfg['frigate']['url'] == 'http://frigate.invalid:5000'
    assert isinstance(relay.camera_resolver_from_config(cfg['frigate']), relay.CameraNameResolver)


# --- 4. Camera display names ----------------------------------------------

def resolver_with(handler, **kwargs):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return relay.CameraNameResolver('http://frigate.invalid:5000/', client=client, **kwargs)


@pytest.mark.asyncio
async def test_camera_names_loaded_from_api_config():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.headers.get('authorization')))
        return httpx.Response(200, json={'cameras': {
            'front_door': {'friendly_name': 'Porte d’entrée'},
            'garage': {},
            'long': {'friendly_name': 'x' * 100},
        }})
    resolver = resolver_with(handler, token='secret-token')
    assert await resolver.refresh()
    assert resolver.display_name('front_door') == 'Porte d’entrée'
    assert resolver.display_name('garage') is None
    assert len(resolver.display_name('long')) == 64
    assert seen == [('GET', '/api/config', 'Bearer secret-token')]
    await resolver.close()


@pytest.mark.asyncio
async def test_camera_names_login_flow():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == '/api/login':
            return httpx.Response(200, headers={'set-cookie': 'frigate_token=abc; Path=/'})
        assert 'frigate_token=abc' in request.headers.get('cookie', '')
        return httpx.Response(200, json={'cameras': {'front_door': {'friendly_name': 'Front'}}})
    resolver = resolver_with(handler, user='u', password='p')
    assert await resolver.refresh()
    assert seen == ['/api/login', '/api/config']
    await resolver.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('handler', [
    lambda r: httpx.Response(401, text='no'),
    lambda r: httpx.Response(200, text='not json'),
    lambda r: (_ for _ in ()).throw(httpx.ConnectError('down')),
])
async def test_camera_names_failure_keeps_previous_map(handler):
    resolver = resolver_with(handler)
    resolver.names = {'front_door': 'Front'}
    assert not await resolver.refresh()
    assert resolver.display_name('front_door') == 'Front'
    await resolver.close()


class StubResolver:
    def __init__(self, names):
        self.names = names

    def display_name(self, camera):
        return self.names.get(camera)

    async def run_forever(self):
        await asyncio.sleep(3600)

    async def close(self):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize('names,expected', [({'front_door': 'Porte'}, 'Porte'), ({}, None)])
async def test_envelope_carries_display_name_only_when_known(monkeypatch, tmp_path, clock, names, expected):
    sent = await run_loop(monkeypatch, tmp_path, incident_steps(), clock, resolver=StubResolver(names))
    assert len(sent) == 1
    assert sent[0].get('camera_display_name') == expected
    if expected is None:
        assert 'camera_display_name' not in sent[0]


def test_direct_payload_prefers_display_name():
    payload = relay.build_apns_payload({'camera': 'front_door', 'label': 'person', 'camera_display_name': 'Porte'})
    assert payload['aps']['alert']['subtitle'] == 'Porte'


def test_version():
    assert relay.RELAY_VERSION == '1.2.2'
