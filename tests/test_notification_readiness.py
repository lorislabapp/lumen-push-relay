import math
import pytest
import relay
from src.review_contract import ReviewCorrelator

NOW=1_800_000_000.0
CONFIG={'filters':{'labels':['person','car'],'min_score':.75,'max_event_age_seconds':120,'review_severities':['alert']}}
def event(i='e', score=.75, label='car', **extra):
    return {'type':'new','after':{'id':i,'camera':'swann_ch16','label':label,'top_score':score,'start_time':NOW-5,'stationary':False,'zones':[],**extra}}
def review(ids=None, **extra):
    return {'type':'new','after':{'id':'r','camera':'swann_ch16','severity':'alert','start_time':NOW-6,'data':{'detections':ids or ['e']},**extra}}
@pytest.fixture
def correlator(monkeypatch):
    monkeypatch.setattr(relay.time,'time',lambda:NOW)
    return ReviewCorrelator(event_filter=lambda e:relay.should_notify(e,CONFIG) is None,allowed_severities=frozenset(['alert']))
@pytest.mark.parametrize('score,label,stationary,allowed',[(.71,'car',False,False),(.74,'car',False,False),(.75,'car',False,True),(.76,'person',False,True),(.99,'dog',False,False),(.99,'car',True,False)])
def test_notification_eligibility(correlator,score,label,stationary,allowed):
    assert correlator.ingest_event_message(event(score=score,label=label,stationary=stationary),now=NOW)==[]
    result=correlator.ingest_review_message(review(),now=NOW)
    assert bool(result)==allowed
    if result: assert result[0]['stationary'] is False

def test_later_confidence_crossing_can_resolve_pending_review(correlator):
    correlator.ingest_event_message(event(score=.71),now=NOW)
    assert correlator.ingest_review_message(review(),now=NOW)==[]
    update=event(score=.75);update['type']='update'
    assert len(correlator.ingest_event_message(update,now=NOW+1))==1

def test_selects_eligible_person_instead_of_highest_scoring_dog(correlator):
    correlator.ingest_event_message(event('dog',.99,'dog'),now=NOW)
    correlator.ingest_event_message(event('person',.8,'person'),now=NOW)
    assert correlator.ingest_review_message(review(['dog','person']),now=NOW)[0]['eventId']=='person'

def test_cross_camera_candidate_cannot_hide_correct_candidate(correlator):
    correlator.ingest_event_message(event('other',.99,camera='other'),now=NOW)
    correlator.ingest_event_message(event('e',.8),now=NOW)
    assert correlator.ingest_review_message(review(['other','e']),now=NOW)[0]['eventId']=='e'

def test_detection_waits_until_alert_escalation(correlator):
    correlator.ingest_event_message(event(),now=NOW)
    assert correlator.ingest_review_message(review(severity='detection'),now=NOW)==[]
    assert len(correlator.ingest_review_message(review(),now=NOW))==1

def test_suppressed_review_never_becomes_an_enrichment(correlator):
    correlator.ingest_event_message(event(),now=NOW)
    envelope=correlator.ingest_review_message(review(),now=NOW)[0]
    correlator.complete_delivery(envelope,terminal=True,notified=False,now=NOW)
    assert correlator.ingest_identity_update({'type':'description','id':'e','description':'Parked car'},now=NOW+1)==[]

def test_cooldown_failure_is_retryable(monkeypatch):
    monkeypatch.setattr(relay.time,'time',lambda:NOW)
    tracker=relay.CooldownTracker(120)
    assert tracker.check_and_claim('camera','car')
    assert not tracker.check_and_claim('camera','car')
    tracker.release('camera','car')
    assert tracker.check_and_claim('camera','car')

@pytest.mark.parametrize('value',[True,float('nan'),float('inf'),-.1,1.1,None])
def test_invalid_confidence_is_never_eligible(correlator,value):
    assert correlator.ingest_event_message(event(score=value),now=NOW)==[]
    assert correlator.ingest_review_message(review(),now=NOW)==[]
    assert relay.should_notify(event(score=value)['after'],CONFIG) is not None

def test_pending_review_expires_before_new_event_can_resolve_it(correlator):
    # 1.2.2: an open Review stays eligible up to the 15-minute hard cap
    # (was 120 s); past the cap a new event can no longer resolve it.
    assert correlator.ingest_review_message(review(),now=NOW)==[]
    assert correlator.ingest_event_message(event(start_time=NOW+901),now=NOW+901)==[]

def test_current_zones_are_preserved(correlator):
    correlator.ingest_event_message(event(current_zones=['car_presence']),now=NOW)
    assert correlator.ingest_review_message(review(),now=NOW)[0]['zones']==['car_presence']

def test_direct_payload_carries_filter_evidence_and_silent_enrichment():
    e={'eventId':'e','reviewId':'r','reviewSeverity':'alert','sourceSchema':'frigate-review-v1','score':.75,'stationary':False,'isEnrichment':True}
    p=relay.build_apns_payload(e)
    assert p['top_score']==.75
    assert p['stationary'] is False
    assert p['aps']['interruption-level']=='passive'
    assert 'sound' not in p['aps']
