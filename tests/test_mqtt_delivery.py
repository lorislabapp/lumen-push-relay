import asyncio
import json
from types import SimpleNamespace
import pytest
import relay

NOW = 1_800_000_000.0

def event(score=.9, stationary=False, kind='new'):
    return ('custom/events', {'type':kind,'after':{'id':'e','camera':'front','label':'car',
            'top_score':score,'stationary':stationary,'start_time':NOW-5,'zones':[]}})

def review(severity='alert'):
    return ('custom/reviews', {'type':'new','after':{'id':'r','camera':'front',
            'severity':severity,'start_time':NOW-6,'data':{'detections':['e']}}})

async def run_messages(monkeypatch, tmp_path, messages, results=None):
    sent, subscribed = [], []
    outcomes = iter(results or [])
    class Sender:
        closed=False
        async def send(self, envelope):
            sent.append(envelope)
            return next(outcomes,True)
        async def close(self):self.closed=True
    sender=Sender()
    class Client:
        def __init__(self, **options): self.messages=self.stream()
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def subscribe(self,topic):subscribed.append(topic)
        async def stream(self):
            for topic,value in messages:
                yield SimpleNamespace(topic=topic,payload=json.dumps(value).encode())
            raise asyncio.CancelledError()
    config={'mode':'worker','worker':{'push_urls':['https://push.example.invalid/owned']},
            'frigate':{'mqtt_host':'broker.invalid','mqtt_port':1883,'topic':'custom/reviews'},
            'filters':{'labels':['car','person'],'min_score':.8,'max_event_age_seconds':120,
                       'review_severities':['alert'],'cooldown_seconds':0}}
    monkeypatch.setattr(relay,'load_config',lambda:config)
    monkeypatch.setattr(relay,'WorkerSender',lambda urls:sender)
    monkeypatch.setattr(relay.aiomqtt,'Client',Client)
    monkeypatch.setattr(relay.time,'time',lambda:NOW)
    monkeypatch.setenv('LUMEN_REVIEW_STATE_PATH',str(tmp_path/'state.json'))
    with pytest.raises(asyncio.CancelledError):await relay.run()
    assert sender.closed
    assert set(subscribed)=={'custom/events','custom/reviews','custom/tracked_object_update'}
    return sent

@pytest.mark.asyncio
@pytest.mark.parametrize('messages',[[event()],[event(.76),review()],[event(.99,True),review()],
                                     [event(),review('detection')]])
async def test_mqtt_does_not_send_ineligible_or_raw_events(monkeypatch,tmp_path,messages):
    assert await run_messages(monkeypatch,tmp_path,messages)==[]

@pytest.mark.asyncio
@pytest.mark.parametrize('messages',[[event(),review(),review()],[review(),event(),review()],
                                     [event(.76),review(),event(.9,kind='update'),review()]])
async def test_mqtt_correlates_order_and_confidence_once(monkeypatch,tmp_path,messages):
    sent=await run_messages(monkeypatch,tmp_path,messages)
    assert len(sent)==1
    assert sent[0]['sourceSchema']=='frigate-review-v1'
    assert sent[0]['reviewId']=='r' and sent[0]['eventId']=='e'
    assert sent[0]['score']==.9

@pytest.mark.asyncio
async def test_mqtt_failed_delivery_does_not_consume_retry(monkeypatch,tmp_path):
    sent=await run_messages(monkeypatch,tmp_path,[event(),review(),event(kind='update')],[False,True])
    assert len(sent)==2
