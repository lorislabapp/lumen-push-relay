import copy
import time
import pytest
import relay

NOW = 1_800_000_000.0

def event(score=.75, label='car', event_id='e', stationary=False):
    return {'type':'new','after':{'id':event_id,'camera':'swann_ch16','label':label,
        'top_score':score,'start_time':NOW-5,'stationary':stationary,'zones':[]}}

def review():
    return {'type':'new','after':{'id':'r','camera':'swann_ch16','severity':'alert',
        'start_time':NOW-6,'data':{'detections':['e']}}}

class Sender:
    def __init__(self):
        self.sent=[]
        self.accept=True
    async def send(self, value):
        self.sent.append(copy.deepcopy(value))
        return self.accept

@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(time,'time',lambda:NOW)
    monkeypatch.setenv('LUMEN_REVIEW_STATE_PATH',str(tmp_path/'state.json'))
    config={'frigate':{'topic':'frigate/events'},'filters':{
        'labels':['person','car'],'min_score':.75,'review_severities':['alert']}}
    return relay.NotificationPipeline(config,Sender())

def test_legacy_custom_topics_preserve_prefix_and_never_allow_raw_push():
    assert relay.effective_mqtt_topic({'frigate':{'topic':'custom/events','allow_raw_events':True}})==('custom/reviews',True)
    with pytest.raises(ValueError):
        relay.effective_mqtt_topic({'frigate':{'topic':'wrong'}})

@pytest.mark.asyncio
async def test_raw_detection_never_sends_without_review(pipeline):
    await pipeline.handle('frigate/events',event())
    assert pipeline.sender.sent==[]

@pytest.mark.asyncio
@pytest.mark.parametrize('score,label,stationary,expected',[(.749,'car',False,0),(.75,'car',False,1),(.8,'person',False,1),(.99,'dog',False,0),(.99,'car',True,0)])
async def test_real_pipeline_filters_before_transport(pipeline,score,label,stationary,expected):
    await pipeline.handle('frigate/reviews',review())
    await pipeline.handle('frigate/events',event(score,label,stationary=stationary))
    assert len(pipeline.sender.sent)==expected
    if expected:
        envelope=pipeline.sender.sent[0]
        assert envelope['score']==score
        assert envelope['eventId']=='e' and envelope['reviewId']=='r'
        assert envelope['sourceSchema']=='frigate-review-v1'

@pytest.mark.asyncio
async def test_confidence_crossing_and_reconnect_deduplicate(pipeline):
    await pipeline.handle('frigate/events',event(.71))
    await pipeline.handle('frigate/reviews',review())
    assert pipeline.sender.sent==[]
    update=event(.76);update['type']='update'
    await pipeline.handle('frigate/events',update)
    await pipeline.handle('frigate/reviews',review())
    assert len(pipeline.sender.sent)==1
    restarted=relay.NotificationPipeline(pipeline.config,pipeline.sender)
    await restarted.handle('frigate/events',update)
    await restarted.handle('frigate/reviews',review())
    assert len(pipeline.sender.sent)==1

@pytest.mark.asyncio
async def test_transport_failure_releases_cooldown_and_claim(pipeline):
    pipeline.sender.accept=False
    await pipeline.handle('frigate/events',event())
    await pipeline.handle('frigate/reviews',review())
    pipeline.sender.accept=True
    await pipeline.handle('frigate/reviews',review())
    assert len(pipeline.sender.sent)==2
    await pipeline.handle('frigate/reviews',review())
    assert len(pipeline.sender.sent)==2

@pytest.mark.asyncio
async def test_worker_posts_canonical_envelope_without_raw_wrapper(monkeypatch):
    sender=relay.WorkerSender(['https://example.invalid/v2/notify/redacted'])
    captured=[]
    async def capture(url,payload): captured.append(payload); return True
    monkeypatch.setattr(sender,'_send_one',capture)
    canonical={'sourceSchema':'frigate-review-v1','reviewId':'r','score':.75}
    assert await sender.send(canonical)
    assert captured==[canonical]
    await sender.close()

def test_yaml_configuration_takes_precedence_and_env_aliases(tmp_path,monkeypatch):
    config=tmp_path/'config.yaml';config.write_text('filters:\n  min_score: 0.8\n')
    monkeypatch.setenv('CONFIG_PATH',str(config))
    monkeypatch.setenv('FILTER_MIN_SCORE','0.75')
    assert relay.load_config()['filters']['min_score']==.8
    monkeypatch.setenv('CONFIG_PATH',str(tmp_path/'missing'))
    monkeypatch.setenv('REVIEW_SEVERITIES','alert')
    assert relay.load_config()['filters']['review_severities']==['alert']
    monkeypatch.setenv('FILTER_REVIEW_SEVERITIES','alert,detection')
    assert relay.load_config()['filters']['review_severities']==['alert','detection']

@pytest.mark.asyncio
async def test_direct_partial_retry_does_not_resend_successful_device(tmp_path,monkeypatch):
    import httpx
    key=tmp_path/'test-key';key.write_text('fixture, never signed')
    sender=relay.DirectAPNsSender(str(key),'key','team','bundle','sandbox',['device-a','device-b'])
    monkeypatch.setattr(sender,'_get_token',lambda:'fixture')
    requests=[]
    async def post(url,**kwargs):
        requests.append((url,kwargs))
        return httpx.Response(503 if len(requests)==2 else 200)
    monkeypatch.setattr(sender._client,'post',post)
    envelope={'eventId':'e','reviewId':'r','reviewSeverity':'alert','sourceSchema':'frigate-review-v1',
        'camera':'cam','label':'person','score':.8,'detected_at':NOW,'stationary':False}
    assert not await sender.send(envelope)
    assert await sender.send(envelope)
    assert [url.rsplit('/',1)[-1] for url,_ in requests]==['device-a','device-b','device-b']
    assert len(requests[0][1]['headers']['apns-collapse-id'])==64
    await sender.close()

def test_quoted_env_urls_are_imported_without_quote_characters(monkeypatch):
    monkeypatch.setenv('PUSH_URLS','"https://example.invalid/v2/notify/fixture"')
    assert relay._collect_push_urls_from_env()==['https://example.invalid/v2/notify/fixture']
