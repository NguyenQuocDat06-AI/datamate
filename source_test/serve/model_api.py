"""Reusable pinned-model API, with private credentials and on-demand Kaggle lifecycle.

No endpoint or token is printed. `export` writes mode-600 client files for run_v2.
The stable local gateway is optional; the Python client also discovers new URLs.
"""
from __future__ import annotations
import argparse,ast,base64,contextlib,fcntl,gzip,hashlib,hmac,http.server,importlib.util,io,json,os,secrets,shutil,sys,tarfile,tempfile,threading,time,uuid
from pathlib import Path
from urllib.parse import urlsplit
import httpx

ROOT=Path(__file__).resolve().parent
DEFAULT_STATE=Path('/home/ubuntu/datamate_review_data/model_api_state')
AUTH_HELPER=Path('/home/ubuntu/datamate_review_data/gen_kaggle_auth.py')
MODEL='google/gemma-4-12B-it'
_AUTH_LOCK=threading.RLock()


class ServiceUnavailable(RuntimeError):pass
class CapacityBusy(ServiceUnavailable):pass


def canonical(data):return json.dumps(data,sort_keys=True,separators=(',',':')).encode()


def valid_endpoint(url):
    try:
        parts=urlsplit(url)
        return (parts.scheme=='https' and parts.hostname and parts.hostname.endswith('.trycloudflare.com')
                and not parts.username and not parts.password and parts.port in (None,443)
                and parts.path.rstrip('/')=='/v1' and not parts.query and not parts.fragment)
    except (ValueError,TypeError):return False


def verified_announcement(data,cfg,now=None):
    if not isinstance(data,dict):return False
    unsigned={k:v for k,v in data.items() if k!='signature'}
    signature=hmac.new(cfg['token'].encode(),canonical(unsigned),hashlib.sha256).hexdigest()
    return (isinstance(data.get('signature'),str) and hmac.compare_digest(signature,data['signature'])
            and data.get('instance_id')==cfg.get('instance_id') and data.get('revision')==cfg['revision']
            and data.get('model')==cfg['model'] and valid_endpoint(data.get('base_url'))
            and isinstance(data.get('published_at'),(int,float))
            and -60<=(now or time.time())-data['published_at']<600)


def private_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);path.parent.chmod(0o700)
    fd,name=tempfile.mkstemp(prefix='.api_',dir=path.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w') as f:json.dump(value,f,indent=2);f.write('\n')
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


class ModelApiClient:
    def __init__(self,state_dir=None,transport=None):
        self.state_dir=Path(state_dir or os.environ.get('DATAMATE_MODEL_API_STATE',DEFAULT_STATE))
        self.state_dir.mkdir(parents=True,exist_ok=True);self.state_dir.chmod(0o700)
        self.path=self.state_dir/'service.json'
        self.http=httpx.Client(timeout=20,follow_redirects=False,transport=transport)
        self._thread_lock=threading.RLock()

    def close(self):self.http.close()
    def _load(self):
        if not self.path.exists():raise ServiceUnavailable('service is not initialized; run init first')
        return json.loads(self.path.read_text())
    def _save(self,cfg):private_json(self.path,cfg)

    @contextlib.contextmanager
    def _lock(self):
        with self._thread_lock,(self.state_dir/'lifecycle.lock').open('a') as handle:
            fcntl.flock(handle,fcntl.LOCK_EX)
            try:yield
            finally:fcntl.flock(handle,fcntl.LOCK_UN)

    def initialize(self,account=1,session_seconds=21600,idle_seconds=1800):
        if session_seconds<900 or session_seconds>42600 or (idle_seconds and idle_seconds<600):
            raise ValueError('session must be 15 minutes to 11h50; idle is zero or at least ten minutes')
        with self._lock():
            if self.path.exists():return {'initialized':True,'state_dir':str(self.state_dir),'already_exists':True}
            manifest=json.loads((ROOT/'model_manifest.json').read_text())
            weight=next(item for item in manifest['files'] if item['path']=='model.safetensors')
            cfg={'account':account,'session_seconds':session_seconds,'idle_seconds':idle_seconds,
                 'token':secrets.token_hex(32),'relay':'dm-api-'+secrets.token_hex(20),
                 'model':manifest['repo'],'revision':manifest['revision'],'weights_sha256':weight['sha256'],
                 'slug':'dm-gemma4-reusable-api','instance_id':None,'phase':'stopped','desired':'stopped'}
            self._save(cfg)
        return {'initialized':True,'state_dir':str(self.state_dir)}

    @contextlib.contextmanager
    def _kaggle(self,cfg):
        # Kaggle's existing authentication helper changes process-wide environment.
        with _AUTH_LOCK:
            with self._authenticated_kaggle(cfg) as value:yield value

    @contextlib.contextmanager
    def _authenticated_kaggle(self,cfg):
        helper_path=Path(os.environ.get('DATAMATE_KAGGLE_AUTH_HELPER',AUTH_HELPER))
        spec=importlib.util.spec_from_file_location('datamate_api_kaggle_auth',helper_path)
        helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
        previous=os.environ.get('KAGGLE_CONFIG_DIR');silent=io.StringIO()
        try:
            with contextlib.redirect_stdout(silent),contextlib.redirect_stderr(silent):
                api=helper.api(cfg['account'])
                yield api,helper.kernel_id(cfg['account'],cfg['slug'])
        finally:
            conf=os.environ.get('KAGGLE_CONFIG_DIR')
            if conf and conf!=previous and Path(conf).name.startswith(f"kaggle_acc{cfg['account']}_"):
                shutil.rmtree(conf,ignore_errors=True)
            if previous is None:os.environ.pop('KAGGLE_CONFIG_DIR',None)
            else:os.environ['KAGGLE_CONFIG_DIR']=previous

    def _metadata(self,url,cfg):
        response=self.http.get(url.rstrip('/')+'/metadata',headers={'Authorization':'Bearer '+cfg['token']},timeout=10)
        response.raise_for_status();data=response.json()
        if not (data.get('ready') is True and data.get('weights_verified') is True
                and data.get('instance_id')==cfg.get('instance_id') and data.get('model')==cfg['model']
                and data.get('revision')==cfg['revision'] and data.get('weights_sha256')==cfg['weights_sha256']
                and type(data.get('parameters')) is int and 0<data['parameters']<=15_000_000_000
                and type(data.get('expires_at')) in (int,float)
                and data.get('expires_at',0)>time.time()):
            raise ServiceUnavailable('backend identity or checkpoint verification failed')
        return data

    def _discover(self,cfg):
        candidates=[]
        if valid_endpoint(cfg.get('endpoint')):
            try:return cfg['endpoint'],self._metadata(cfg['endpoint'],cfg)
            except (httpx.HTTPError,ValueError,ServiceUnavailable):pass
        # The private topic is small; enforce freshness on the signed timestamp.
        # Reading its cache also avoids duration-filter clock differences.
        response=self.http.get('https://ntfy.sh/'+cfg['relay']+'/json',params={'poll':'1','since':'all'},timeout=15)
        response.raise_for_status()
        for line in response.text.splitlines():
            try:
                item=json.loads(line);data=json.loads(item.get('message','{}'))
                if verified_announcement(data,cfg):candidates.insert(0,data['base_url'])
            except (ValueError,TypeError):continue
        for url in dict.fromkeys(candidates):
            try:return url,self._metadata(url,cfg)
            except (httpx.HTTPError,ValueError,ServiceUnavailable):continue
        return None,None

    def status(self):
        cfg=self._load()
        if not cfg.get('instance_id'):return {'state':'stopped','model':cfg['model'],'revision':cfg['revision']}
        if cfg.get('desired')=='stopped':
            try:
                try:url,meta=self._discover(cfg)
                except httpx.HTTPError:url=None
                if url:
                    self.http.post(url.removesuffix('/v1')+'/admin/shutdown',headers={
                        'Authorization':'Bearer '+cfg['token'],'X-Datamate-Instance':cfg['instance_id']},timeout=10).raise_for_status()
                with self._kaggle(cfg) as (api,ref):
                    status=api.kernels_status(ref)
                    terminal=str(status.status).split('.')[-1] in ('COMPLETE','ERROR','CANCEL_ACKNOWLEDGED')
                if terminal:
                    with self._lock():
                        fresh=self._load()
                        if fresh.get('instance_id')==cfg['instance_id']:
                            fresh.update(last_instance_id=cfg['instance_id'],instance_id=None,phase='stopped',endpoint=None)
                            self._save(fresh)
                return {'state':'stopped' if terminal else 'stopping','ready':False,'instance_id':cfg['instance_id']}
            except (httpx.HTTPError,ValueError):
                return {'state':'stopping','ready':False,'instance_id':cfg['instance_id']}
        try:
            url,meta=self._discover(cfg)
            if url:
                with self._lock():
                    fresh=self._load()
                    if fresh.get('instance_id')==cfg['instance_id'] and fresh.get('desired')=='running':
                        fresh.update(endpoint=url,phase='ready',last_verified_at=time.time());self._save(fresh)
                    else:return {'state':'stopping','ready':False}
                return {'state':'ready',**meta}
        except (httpx.HTTPError,ValueError):pass
        phase=cfg.get('phase','unknown')
        return {'state':'unavailable' if phase=='ready' else phase,'ready':False,'model':cfg['model'],
                'revision':cfg['revision'],'instance_id':cfg['instance_id']}

    def _program(self,cfg):
        data={'local_server.py':(ROOT/'local_server.py').read_bytes(),
              'model_manifest.json':(ROOT/'model_manifest.json').read_bytes()}
        buf=io.BytesIO()
        with tarfile.open(fileobj=buf,mode='w:xz',preset=6) as tar:
            for name,raw in data.items():
                item=tarfile.TarInfo(name);item.size=len(raw);item.mtime=0
                tar.addfile(item,io.BytesIO(raw))
        code=(ROOT/'kaggle_model_job.py').read_text()
        code=code.replace("'__PUBLIC_PAYLOAD__'",repr(base64.b64encode(buf.getvalue()).decode()))
        code=code.replace("'__PRIVATE_SETTINGS__'",repr(base64.b64encode(canonical(cfg)).decode()))
        ast.parse(code)
        return code

    def start(self):
        if self.status().get('state')=='ready':return {'state':'ready'}
        with self._lock():
            cfg=self._load()
            # A push can precede the notebook's appearance in Kaggle's listing.
            # The local receipt prevents simultaneous cold requests pushing twice.
            if (cfg.get('phase')=='starting' and cfg.get('desired')=='running'
                    and cfg.get('source_sha256') and time.time()-cfg.get('started_at',0)<900):
                return {'state':'starting','instance_id':cfg['instance_id']}
            with self._kaggle(cfg) as (api,ref):
                kernels=api.kernels_list(mine=True,page_size=100) or []
                own=next((k for k in kernels if k.ref==ref),None)
                # Other active GPU jobs on the account; a state may allow up to max_account_sessions (default 1: none).
                others=0;allowed=max(1,int(cfg.get('max_account_sessions',1)))
                for kernel in kernels:
                    try:value=api.kernels_status(kernel.ref)
                    except Exception as exc:
                        # Old drafts can be listed without any executable session.
                        # All other failures still prevent a new GPU allocation.
                        if getattr(getattr(exc,'response',None),'status_code',None)==404:
                            if kernel.ref==ref:raise ServiceUnavailable('owned service session cannot be verified') from None
                            continue
                        raise
                    fields=value.to_dict()
                    phase=str(value.status).split('.')[-1] if fields else 'UNKNOWN'
                    if phase in ('RUNNING','QUEUED','UNKNOWN'):
                        if kernel.ref==ref:
                            if cfg.get('desired')=='stopped':raise CapacityBusy('previous service is stopping')
                            cfg.update(phase='starting',desired='running');self._save(cfg)
                            return {'state':'starting','instance_id':cfg.get('instance_id')}
                        others+=1
                        if others>=allowed:raise CapacityBusy('another GPU job is active on the configured account')
                if own is not None and cfg.get('source_sha256'):
                    from kagglesdk.kernels.types.kernels_api_service import ApiGetKernelRequest
                    req=ApiGetKernelRequest();req.user_name,req.kernel_slug=ref.split('/',1)
                    with api.build_kaggle_client() as k:source=k.kernels.kernels_api_client.get_kernel(req)
                    digest=hashlib.sha256(source.blob.source.rstrip().encode()).hexdigest() if source.blob else None
                    if digest!=cfg['source_sha256']:raise ServiceUnavailable('existing service source changed outside this controller')
                elif own is not None:
                    raise ServiceUnavailable('kernel exists without an ownership receipt')
                if os.environ.get('DATAMATE_IGNORE_GPU_QUOTA')!='1':  # explicit operator opt-in: Kaggle itself stops the job when quota runs out
                    with api.build_kaggle_client() as k:quota=k.kernels.kernels_api_client.get_accelerator_quota_statistics().gpu_quota
                    if quota is None or quota.total_time_allowed is None or quota.time_used is None:
                        raise ServiceUnavailable('GPU quota is not available')
                    available=(quota.total_time_allowed-quota.time_used).total_seconds()
                    if quota.time_reserved is not None:available-=quota.time_reserved.total_seconds()
                    if available<cfg['session_seconds']+180:raise ServiceUnavailable('insufficient GPU quota for configured session')
                cfg.update(instance_id=str(uuid.uuid4()),phase='starting',desired='running',endpoint=None,started_at=time.time())
                program=self._program(cfg);cfg['source_sha256']=hashlib.sha256(program.rstrip().encode()).hexdigest();self._save(cfg)
                with tempfile.TemporaryDirectory(prefix='dm_api_push_') as folder:
                    directory=Path(folder);directory.chmod(0o700)
                    (directory/'api_job.py').write_text(program);(directory/'api_job.py').chmod(0o600)
                    metadata={'id':ref,'title':cfg['slug'].replace('-',' ').title(),'code_file':'api_job.py','language':'python',
                              'kernel_type':'script','is_private':True,'enable_gpu':True,'enable_internet':True,
                              'dataset_sources':[],'competition_sources':[],'kernel_sources':[],'model_sources':[]}
                    (directory/'kernel-metadata.json').write_text(json.dumps(metadata))
                    pushed=api.kernels_push(folder,timeout=str(cfg['session_seconds']+180),acc='NvidiaTeslaT4')
                if pushed.error:
                    cfg.update(phase='error',last_failure='Kaggle push rejected');self._save(cfg)
                    raise ServiceUnavailable('Kaggle rejected the service job')
                cfg.update(kernel_version=pushed.version_number,kernel_id=pushed.kernel_id);self._save(cfg)
        return {'state':'starting','instance_id':cfg['instance_id'],'kernel_version':cfg['kernel_version']}

    def ensure_ready(self,timeout=900):
        until=time.monotonic()+timeout;next_start=0
        while time.monotonic()<until:
            state=self.status()
            if state.get('state')=='ready':return state
            if time.monotonic()>=next_start:
                try:self.start()
                except CapacityBusy:pass
                next_start=time.monotonic()+30
            time.sleep(5)
        raise ServiceUnavailable('GPU API did not become ready before the wait deadline')

    def chat(self,messages,*,max_tokens=256,temperature=0.0,timeout=900,**options):
        self.ensure_ready(timeout)
        cfg=self._load();url=cfg['endpoint']
        # Verify the instance immediately before forwarding a user request.
        self._metadata(url,cfg)
        response=self.http.post(url+'/chat/completions',headers={'Authorization':'Bearer '+cfg['token']},
            json={'model':cfg['model'],'messages':messages,'max_tokens':max_tokens,'temperature':temperature,**options},timeout=600)
        response.raise_for_status();return response.json()

    def export(self,out,local_gateway=False):
        if not local_gateway:self.ensure_ready()
        cfg=self._load();out=Path(out)
        private_json(out/'connection.json',{'model':cfg['model'],'bearer_token':cfg['token']})
        private_json(out/'endpoint.json',{'base_url':'http://127.0.0.1:8010/v1' if local_gateway else cfg['endpoint']})
        return {'connection_file':str(out/'connection.json'),'endpoint_file':str(out/'endpoint.json')}

    def stop(self):
        # Record intent before any network request, so an outage cannot lose stop.
        with self._lock():
            cfg=self._load()
            if not cfg.get('instance_id'):return {'state':'stopped'}
            cfg.update(phase='stopping',desired='stopped');self._save(cfg)
        try:
            url,meta=self._discover(cfg)
            if url:
                response=self.http.post(url.removesuffix('/v1')+'/admin/shutdown',headers={
                    'Authorization':'Bearer '+cfg['token'],'X-Datamate-Instance':cfg['instance_id']},timeout=15)
                response.raise_for_status()
                return {'state':'stop_requested','instance_id':cfg['instance_id']}
        except httpx.HTTPError:pass
        return {'state':'stopping','instance_id':cfg['instance_id']}


def run_gateway(client,port=8010,stop_event=None):
    """Stable loopback API. Cold chat requests wait for startup; health never starts GPU."""
    token=client._load()['token']
    stop_event=stop_event or threading.Event()
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def authorized(self):
            value=self.headers.get('Authorization','').split()
            return len(value)==2 and value[0].lower()=='bearer' and hmac.compare_digest(value[1],token)
        def send(self,status,value):
            body=json.dumps(value).encode();self.send_response(status)
            self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)))
            self.end_headers();self.wfile.write(body)
        def do_GET(self):
            if not self.authorized():return self.send(401,{'error':{'type':'auth_error','message':'Unauthorized'}})
            if self.path.rstrip('/') not in ('/health','/v1/metadata','/v1/models'):
                return self.send(404,{'error':{'type':'not_found'}})
            try:
                state=client.status()
                if self.path.rstrip('/')=='/v1/models':
                    if state['state']!='ready':return self.send(503,{'error':{'type':'model_not_ready','message':state['state']}})
                    return self.send(200,{'object':'list','data':[{'id':state['model'],'object':'model'}]})
                return self.send(200,state)
            except Exception:return self.send(503,{'error':{'type':'service_unavailable'}})
        def do_POST(self):
            if not self.authorized():return self.send(401,{'error':{'type':'auth_error','message':'Unauthorized'}})
            try:
                if self.path=='/admin/start':
                    threading.Thread(target=warm,daemon=True).start()
                    return self.send(202,{'state':'start_requested'})
                if self.path=='/admin/shutdown':return self.send(200,client.stop())
                if self.path!='/v1/chat/completions':return self.send(404,{'error':{'type':'not_found'}})
                size=int(self.headers.get('Content-Length','0'))
                if size<1 or size>2*1024*1024:return self.send(413,{'error':{'type':'request_too_large'}})
                body=json.loads(self.rfile.read(size));messages=body.get('messages')
                if not isinstance(messages,list):return self.send(400,{'error':{'type':'invalid_request'}})
                if not 1<=int(body.get('max_tokens',256))<=8192:return self.send(400,{'error':{'type':'invalid_request'}})
                result=client.chat(messages,max_tokens=int(body.get('max_tokens',256)),temperature=body.get('temperature',0))
                if body.get('stream'):
                    choices=[{'index':choice.get('index',0),'delta':choice.get('message',{}),
                              'finish_reason':choice.get('finish_reason')} for choice in result.get('choices',[])]
                    data={'id':result.get('id'),'object':'chat.completion.chunk','model':result.get('model'),'choices':choices}
                    payload=('data: '+json.dumps(data)+'\n\ndata: [DONE]\n\n').encode()
                    self.send_response(200);self.send_header('Content-Type','text/event-stream')
                    self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
                    return
                return self.send(200,result)
            except (ValueError,TypeError):return self.send(400,{'error':{'type':'invalid_request'}})
            except Exception:return self.send(503,{'error':{'type':'service_unavailable','message':'Model API is not ready'}})
    def warm():
        try:client.ensure_ready(timeout=1800)
        except Exception:pass
    def observe():
        while not stop_event.is_set():
            try:client.status()
            except Exception:pass
            stop_event.wait(30)
    threading.Thread(target=observe,daemon=True).start()
    server=http.server.ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.daemon_threads=True
    def shutdown():
        stop_event.wait();server.shutdown()
    threading.Thread(target=shutdown,daemon=True).start()
    try:server.serve_forever(poll_interval=0.2)
    finally:stop_event.set();server.server_close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir',type=Path,default=None)
    sub=parser.add_subparsers(dest='command',required=True)
    init=sub.add_parser('init');init.add_argument('--account',type=int,default=1)
    init.add_argument('--session-minutes',type=int,default=360);init.add_argument('--idle-minutes',type=int,default=30)
    start=sub.add_parser('start');start.add_argument('--wait',action='store_true');start.add_argument('--timeout',type=int,default=900)
    sub.add_parser('status');sub.add_parser('stop')
    chat=sub.add_parser('chat');chat.add_argument('--prompt',required=True);chat.add_argument('--max-tokens',type=int,default=256)
    export=sub.add_parser('export');export.add_argument('--out',type=Path,required=True)
    export.add_argument('--local-gateway',action='store_true')
    gateway=sub.add_parser('gateway');gateway.add_argument('--port',type=int,default=8010)
    args=parser.parse_args(argv);client=ModelApiClient(args.state_dir)
    try:
        if args.command=='init':result=client.initialize(args.account,args.session_minutes*60,args.idle_minutes*60)
        elif args.command=='start':result=client.ensure_ready(args.timeout) if args.wait else client.start()
        elif args.command=='status':result=client.status()
        elif args.command=='stop':result=client.stop()
        elif args.command=='export':result=client.export(args.out,args.local_gateway)
        elif args.command=='gateway':run_gateway(client,args.port);return 0
        else:result=client.chat([{'role':'user','content':args.prompt}],max_tokens=args.max_tokens)
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except (ServiceUnavailable,httpx.HTTPError) as exc:
        print(json.dumps({'error':type(exc).__name__,'message':str(exc) if isinstance(exc,ServiceUnavailable) else 'model API request failed'}));return 1
    except Exception as exc:
        print(json.dumps({'error':type(exc).__name__,'message':'service operation failed; credentials were not printed'}));return 1
    finally:client.close()


if __name__=='__main__':raise SystemExit(main())
