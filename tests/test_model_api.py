"""Service discovery must verify checkpoint, instance, signatures and credentials."""

import hashlib
import hmac
import json
import os
import time
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from serve.model_api import ModelApiClient, ServiceUnavailable, canonical, valid_endpoint, verified_announcement
from serve.local_server import start_gateway


@pytest.fixture
def api(tmp_path):
    client=ModelApiClient(tmp_path/'state')
    client.initialize()
    cfg=client._load()
    cfg.update(token='synthetic-test-token',instance_id='test-instance',desired='running',phase='ready',
               endpoint='https://unit-test.trycloudflare.com/v1')
    client._save(cfg)
    yield client,cfg
    client.close()


def metadata(cfg):
    return {'ready':True,'weights_verified':True,'instance_id':cfg['instance_id'],
            'model':cfg['model'],'revision':cfg['revision'],'weights_sha256':cfg['weights_sha256'],
            'parameters':11_959_730_224,'expires_at':time.time()+3600}


def announcement(cfg,**changes):
    row={'instance_id':cfg['instance_id'],'model':cfg['model'],'revision':cfg['revision'],
         'base_url':cfg['endpoint'],'published_at':time.time(),**changes}
    row['signature']=hmac.new(cfg['token'].encode(),canonical(row),hashlib.sha256).hexdigest()
    return row


def test_credentials_are_private_and_initialization_is_idempotent(api):
    client,cfg=api
    assert client.path.stat().st_mode & 0o777 == 0o600
    assert client.state_dir.stat().st_mode & 0o777 == 0o700
    client.initialize()
    assert client._load()['token']==cfg['token']


@pytest.mark.parametrize('url',['http://unit.trycloudflare.com/v1','https://user@unit.trycloudflare.com/v1',
                               'https://unit.trycloudflare.com.evil.invalid/v1','http://127.0.0.1/v1',
                               'https://unit.trycloudflare.com/v1?token=x'])
def test_registry_cannot_redirect_credentials_to_arbitrary_targets(url):
    assert not valid_endpoint(url)


def test_signed_registry_rejects_tampering_wrong_instance_and_expired_announcements(api):
    _,cfg=api
    original=announcement(cfg)
    assert verified_announcement(original,cfg)
    assert not verified_announcement({**original,'base_url':'https://other.trycloudflare.com/v1'},cfg)
    assert not verified_announcement(announcement(cfg,instance_id='old-instance'),cfg)
    assert not verified_announcement(announcement(cfg,published_at=time.time()-700),cfg)


def test_live_cached_endpoint_survives_registry_outage(api):
    client,cfg=api;seen=[]
    def transport(request):
        seen.append(request.url.host)
        if request.url.host=='ntfy.sh':raise httpx.ConnectError('registry unavailable')
        return httpx.Response(200,json=metadata(cfg))
    client.http.close();client.http=httpx.Client(transport=httpx.MockTransport(transport))
    assert client.status()['state']=='ready'
    assert seen==['unit-test.trycloudflare.com']


def test_unreachable_cached_backend_is_not_reported_ready(api):
    client,cfg=api
    def transport(request):
        if request.url.host=='ntfy.sh':return httpx.Response(200,text='')
        raise httpx.ConnectError('unavailable')
    client.http.close();client.http=httpx.Client(transport=httpx.MockTransport(transport))
    state=client.status()
    assert state['state']=='unavailable' and state['ready'] is False


def test_discovery_recovers_a_changed_url_from_signed_cache(api):
    client,cfg=api
    fresh='https://replacement.trycloudflare.com/v1'
    def transport(request):
        if request.url.host=='unit-test.trycloudflare.com':raise httpx.ConnectError('old tunnel expired')
        if request.url.host=='ntfy.sh':
            assert request.url.params['since']=='all'
            return httpx.Response(200,text=json.dumps({'message':json.dumps(announcement(cfg,base_url=fresh))})+'\n')
        assert request.url.host=='replacement.trycloudflare.com'
        return httpx.Response(200,json=metadata(cfg))
    client.http.close();client.http=httpx.Client(transport=httpx.MockTransport(transport))
    assert client.status()['state']=='ready'
    assert client._load()['endpoint']==fresh


def test_mismatched_checkpoint_never_receives_user_prompt(api,monkeypatch):
    client,cfg=api;seen=[]
    def transport(request):
        seen.append(request.method)
        return httpx.Response(200,json={**metadata(cfg),'revision':'unexpected'})
    client.http.close();client.http=httpx.Client(transport=httpx.MockTransport(transport))
    monkeypatch.setattr(client,'ensure_ready',lambda timeout:None)
    with pytest.raises(ServiceUnavailable,match='verification'):
        client.chat([{'role':'user','content':'synthetic prompt'}])
    assert seen==['GET']


def test_stop_is_persisted_even_when_registry_and_tunnel_are_unavailable(api):
    client,cfg=api
    def transport(request):raise httpx.ConnectError('network outage')
    client.http.close();client.http=httpx.Client(transport=httpx.MockTransport(transport))
    assert client.stop()['state']=='stopping'
    assert client._load()['desired']=='stopped'
    assert client._load()['instance_id']==cfg['instance_id']


def test_gateway_metadata_auth_and_instance_bound_shutdown(tmp_path):
    stopped=threading.Event()
    server,state=start_gateway(0,[],'synthetic-token','test-model',stopped)
    state.metadata={'instance_id':'current-instance','revision':'frozen'}
    state.activity_file=tmp_path/'activity';state.activity_file.touch()
    os.utime(state.activity_file,(1,1))
    state.forward_chat=lambda raw:(200,json.dumps({'choices':[{'message':{'content':'ok'}}]}).encode())
    url=f'http://127.0.0.1:{server.server_port}'
    try:
        with httpx.Client(trust_env=False) as web:
            assert web.get(url+'/v1/metadata').status_code==401
            auth={'Authorization':'Bearer synthetic-token'}
            assert web.get(url+'/v1/metadata',headers=auth).json()['ready'] is True
            assert web.post(url+'/admin/shutdown',headers=auth).status_code==409
            assert not stopped.is_set()
            response=web.post(url+'/v1/chat/completions',headers=auth,json={'messages':[]})
            assert response.status_code==200
            assert state.activity_file.stat().st_mtime>1
            assert web.post(url+'/admin/shutdown',headers={**auth,'X-Datamate-Instance':'current-instance'}).status_code==200
            assert stopped.is_set()
    finally:server.shutdown();server.server_close()


def test_deployment_payload_contains_no_training_or_reference_data(api):
    client,cfg=api
    program=client._program(cfg)
    assert cfg['token'] not in program
    assert 'references.jsonl' not in program
    assert 'fewshot_bank.jsonl' not in program
    compile(program,'private_service_job','exec')


def test_cold_start_receipt_prevents_duplicate_push_before_remote_listing(api,monkeypatch):
    client,cfg=api
    cfg.update(phase='starting',started_at=time.time(),source_sha256='owned-source',endpoint=None)
    client._save(cfg)
    monkeypatch.setattr(client,'status',lambda:{'state':'starting'})
    def unexpected_kaggle(*args):raise AssertionError('a second push must not be attempted')
    monkeypatch.setattr(client,'_kaggle',unexpected_kaggle)
    assert client.start()=={'state':'starting','instance_id':cfg['instance_id']}


def test_missing_draft_session_does_not_hide_an_active_job(api,monkeypatch):
    client,cfg=api
    cfg.update(phase='stopped',started_at=0);client._save(cfg)
    monkeypatch.setattr(client,'status',lambda:{'state':'stopped'})
    class Api:
        def kernels_list(self,**kwargs):
            return [SimpleNamespace(ref='owner/old-draft'),SimpleNamespace(ref='owner/active')]
        def kernels_status(self,ref):
            if ref.endswith('old-draft'):
                response=httpx.Response(404,request=httpx.Request('GET','https://example.invalid'))
                response.raise_for_status()
            return SimpleNamespace(status='RUNNING',to_dict=lambda:{'status':'RUNNING'})
    @contextmanager
    def kaggle(_):yield Api(),'owner/service'
    monkeypatch.setattr(client,'_kaggle',kaggle)
    from serve.model_api import CapacityBusy
    with pytest.raises(CapacityBusy,match='another GPU job'):
        client.start()


def test_missing_owned_session_prevents_a_second_gpu_allocation(api,monkeypatch):
    client,cfg=api
    cfg.update(phase='stopped');client._save(cfg)
    monkeypatch.setattr(client,'status',lambda:{'state':'stopped'})
    class Api:
        def kernels_list(self,**kwargs):return [SimpleNamespace(ref='owner/service')]
        def kernels_status(self,ref):
            httpx.Response(404,request=httpx.Request('GET','https://example.invalid')).raise_for_status()
    @contextmanager
    def kaggle(_):yield Api(),'owner/service'
    monkeypatch.setattr(client,'_kaggle',kaggle)
    with pytest.raises(ServiceUnavailable,match='owned service session'):
        client.start()
