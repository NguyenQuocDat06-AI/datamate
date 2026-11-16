"""Bounded GPU API bootstrap; deployment fills the two placeholders in private memory."""
from __future__ import annotations
import base64,hashlib,hmac,io,json,math,os,re,shutil,signal,subprocess,sys,tarfile,threading,time,urllib.request
from pathlib import Path

PAYLOAD = '__PUBLIC_PAYLOAD__'
SETTINGS = '__PRIVATE_SETTINGS__'


def run():
    cfg=json.loads(base64.b64decode(SETTINGS))
    started=time.time();deadline=started+cfg['session_seconds']
    work=Path('/tmp/datamate_model_api');work.mkdir(parents=True,exist_ok=True)
    result=Path('/kaggle/working/api_status');result.mkdir(parents=True,exist_ok=True)
    children=[];stop=threading.Event()
    def event(stage,**data):
        row={'stage':stage,'elapsed_seconds':round(time.time()-started,2),**data}
        print('DM_API '+json.dumps(row),flush=True)
        (result/'status.json').write_text(json.dumps(row)+'\n')
    def on_signal(*_):stop.set()
    signal.signal(signal.SIGTERM,on_signal);signal.signal(signal.SIGINT,on_signal)
    def check():
        if stop.is_set() or time.time()>=deadline:raise InterruptedError('service stopped or deadline reached')
    def child(cmd,logname,env=None):
        with (work/logname).open('w') as log:
            proc=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT,env=env,start_new_session=True)
        children.append(proc);return proc
    def wait(proc):
        while proc.poll() is None:check();time.sleep(1)
        if proc.returncode:raise RuntimeError('subprocess failed: '+str(proc.returncode))
    def announce(url,status='ready'):
        data={'base_url':url+'/v1','model':cfg['model'],'instance_id':cfg['instance_id'],
              'revision':cfg['revision'],'published_at':time.time(),'status':status}
        canonical=json.dumps(data,sort_keys=True,separators=(',',':')).encode()
        data['signature']=hmac.new(cfg['token'].encode(),canonical,hashlib.sha256).hexdigest()
        req=urllib.request.Request('https://ntfy.sh/'+cfg['relay'],data=json.dumps(data).encode(),
                                  headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=15) as response:return response.status==200
    def tunnel():
        binary=work/'cloudflared'
        if not binary.exists():
            req=urllib.request.Request('https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64',headers={'User-Agent':'DataMateAPI/1'})
            with urllib.request.urlopen(req,timeout=90) as source,binary.open('wb') as target:shutil.copyfileobj(source,target)
            binary.chmod(0o700)
        proc=subprocess.Popen([str(binary),'tunnel','--url','http://127.0.0.1:8000','--no-autoupdate'],
                              stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True,start_new_session=True)
        children.append(proc);found=[];signal_ready=threading.Event()
        def capture():
            for line in proc.stderr:
                match=re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com',line)
                if match and not found:found.append(match.group());signal_ready.set()
        threading.Thread(target=capture,daemon=True).start()
        if not signal_ready.wait(90):raise RuntimeError('tunnel startup failed')
        return proc,found[0]
    try:
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(PAYLOAD)),mode='r:xz') as archive:
            for member in archive:
                path=work/member.name
                if not member.isfile() or not path.resolve().is_relative_to(work.resolve()):raise RuntimeError('invalid payload')
                path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(archive.extractfile(member).read())
        event('installing')
        reqs=['transformers==5.23.0','accelerate==1.15.0','bitsandbytes==0.50.2',
              'safetensors==0.8.0','sentencepiece==0.2.3','pillow==13.0.0','httpx==0.28.1']
        wait(child([sys.executable,'-m','pip','install','-q','--disable-pip-version-check',*reqs],'install.log'))
        event('downloading_model')
        model=work/'model';manifest=json.loads((work/'model_manifest.json').read_text())
        # Run download and verification in a child so the parent can terminate it on shutdown/deadline.
        download="""import json,hashlib,math,sys
+from pathlib import Path
+from huggingface_hub import snapshot_download
+import torch,transformers,bitsandbytes
+manifest=json.loads(Path(sys.argv[1]).read_text());target=Path(sys.argv[2])
+snapshot_download(repo_id=manifest['repo'],revision=manifest['revision'],local_dir=str(target),allow_patterns=[x['path'] for x in manifest['files'] if x['path']!='LICENSE'])
+for item in manifest['files']:
+ if item['path']=='LICENSE':continue
+ h=hashlib.sha256();path=target/item['path']
+ with path.open('rb') as f:
+  while chunk:=f.read(8*1024*1024):h.update(chunk)
+ assert path.stat().st_size==item['size'] and h.hexdigest()==item['sha256'],'checkpoint mismatch'
+with (target/'model.safetensors').open('rb') as f:
+ length=int.from_bytes(f.read(8),'little');header=json.loads(f.read(length))
+params=sum(math.prod(v['shape']) for k,v in header.items() if k!='__metadata__')
+assert params<=15000000000 and torch.cuda.is_available()
+Path(sys.argv[3]).write_text(json.dumps({'parameters':params,'weights_verified':True,'gpu_count':torch.cuda.device_count(),'torch':torch.__version__,'cuda':torch.version.cuda,'transformers':transformers.__version__,'bitsandbytes':bitsandbytes.__version__}))
+""".replace('\n+','\n')
        script=work/'download.py';script.write_text(download)
        verified=work/'verified.json'
        wait(child([sys.executable,str(script),str(work/'model_manifest.json'),str(model),str(verified)],'download.log'))
        metadata={**json.loads(verified.read_text()),'instance_id':cfg['instance_id'],'model':cfg['model'],
                  'revision':cfg['revision'],'weights_sha256':cfg['weights_sha256'],'quantization':'NF4',
                  'compute_dtype':'float16','seed':42,'batch_max':1,'expires_at':deadline,
                  'idle_timeout_seconds':cfg['idle_seconds'],
                  'server_source_sha256':hashlib.sha256((work/'local_server.py').read_bytes()).hexdigest(),
                  'verified_configuration_sha256':{item['path']:item['sha256'] for item in manifest['files']
                      if item['path'].endswith(('.json','.jinja','.model'))}}
        info=work/'public_metadata.json';info.write_text(json.dumps(metadata))
        activity=work/'activity';activity.touch()
        env=dict(os.environ,DATAMATE_SERVER_TOKEN=cfg['token'],DATAMATE_SEED='42',PYTHONHASHSEED='42',
                 HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',NO_PROXY='127.0.0.1,localhost',no_proxy='127.0.0.1,localhost')
        count=min(2,metadata['gpu_count']);event('loading_model',gpu_count=count)
        server=child([sys.executable,str(work/'local_server.py'),'--model-dir',str(model),'--gpus',','.join(map(str,range(count))),
                      '--served-model-name',cfg['model'],'--batch-max','1','--startup-timeout','600',
                      '--metadata-file',str(info),'--activity-file',str(activity)],'model_server.log',env)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        until=time.time()+660
        while time.time()<until:
            check()
            if server.poll() is not None:raise RuntimeError('model server startup failed')
            try:
                request=urllib.request.Request('http://127.0.0.1:8000/v1/metadata',headers={'Authorization':'Bearer '+cfg['token']})
                with opener.open(request,timeout=3) as response:
                    if response.status==200:break
            except Exception:time.sleep(2)
        else:raise RuntimeError('model server startup timeout')
        process,url=tunnel();announce(url);activity.touch();last_publish=time.time()
        event('ready',instance_id=cfg['instance_id'],**{k:metadata[k] for k in ('parameters','revision','gpu_count','weights_verified')})
        while not stop.wait(5):
            check()
            if server.poll() is not None:break
            if cfg['idle_seconds'] and time.time()-activity.stat().st_mtime>cfg['idle_seconds']:
                event('idle_shutdown');break
            if process.poll() is not None:process,url=tunnel();announce(url)
            if time.time()-last_publish>120:
                try:announce(url);last_publish=time.time()
                except Exception:event('registry_retry')
        event('stopping')
    except InterruptedError:event('stopping')
    except BaseException as exc:
        event('failed',category=type(exc).__name__)
        raise
    finally:
        for proc in reversed(children):
            if proc.poll() is None:
                try:os.killpg(proc.pid,signal.SIGTERM)
                except ProcessLookupError:pass
        for proc in reversed(children):
            try:proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                try:os.killpg(proc.pid,signal.SIGKILL)
                except ProcessLookupError:pass
        event('stopped')


if __name__=='__main__':run()
