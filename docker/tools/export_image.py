"""Stream a Docker-save-compatible image with a verified inference-weight layer.

The runtime image is built/tested by Docker. Appending a regular COPY-equivalent
layer avoids materializing 24GB of weights on the 67GB ARM build host. No inference
assets are fetched at inference time. A small scratch-image load test checks this
archive writer with the installed Docker daemon before the full export.
"""
import argparse
import os
import datetime
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import time
import threading

import httpx

ROOT=Path(__file__).resolve().parent
# Docker daemon used for `docker save`: DATAMATE_DOCKER_SOCKET (default: the system socket) and an optional command
# prefix such as 'sudo' in DATAMATE_DOCKER_PREFIX.
SOCKET=os.environ.get('DATAMATE_DOCKER_SOCKET','unix:///var/run/docker.sock')
PREFIX=os.environ.get('DATAMATE_DOCKER_PREFIX','').split()


class Reader:
    def __init__(self, iterator):
        self.iterator=iter(iterator);self.buffer=memoryview(b'');self.position=0
    def read(self,size):
        parts=[];remaining=size
        while remaining:
            if self.position==len(self.buffer):
                try:self.buffer=memoryview(next(self.iterator));self.position=0
                except StopIteration:break
            count=min(remaining,len(self.buffer)-self.position)
            parts.append(self.buffer[self.position:self.position+count].tobytes())
            self.position+=count;remaining-=count
        return b''.join(parts)


class CountingWriter:
    def __init__(self,stream):
        self.stream=stream;self.hash=hashlib.md5();self.bytes=0
    def write(self,data):
        self.hash.update(data);self.bytes+=len(data)
        return self.stream.write(data)
    def flush(self):self.stream.flush()


def info(name,size=0,directory=False):
    item=tarfile.TarInfo(name);item.size=size;item.uid=item.gid=0;item.mtime=0
    item.uname=item.gname='root';item.mode=0o755 if directory else 0o644
    if directory:item.type=tarfile.DIRTYPE
    return item


def chunks(item,local):
    digest=hashlib.sha256();offset=0;size=item['size'];last=time.monotonic()
    if local:
        with local.open('rb') as handle:
            while data:=handle.read(4*1024*1024):
                offset+=len(data);digest.update(data);yield data
    else:
        url=item['source_url']+'?download=true'
        for attempt in range(5):
            try:
                with httpx.stream('GET',url,headers={'Range':f'bytes={offset}-'} if offset else {},
                                  follow_redirects=True,timeout=httpx.Timeout(120,connect=30)) as response:
                    response.raise_for_status()
                    if offset and response.status_code!=206:raise RuntimeError('resume was not honored')
                    for data in response.iter_bytes(4*1024*1024):
                        offset+=len(data)
                        if offset>size:raise RuntimeError('model response exceeded declared size')
                        digest.update(data);yield data
                        if time.monotonic()-last>=20:
                            print(json.dumps({'stage':'weight_layer','bytes':offset,'total':size}),file=sys.stderr,flush=True)
                            last=time.monotonic()
                if offset==size:break
            except (httpx.HTTPError,OSError) as exc:
                print(json.dumps({'stage':'model_download_retry','attempt':attempt+1,'offset':offset,'category':type(exc).__name__}),file=sys.stderr,flush=True)
                if attempt==4:raise RuntimeError('model download failed') from None
        if offset!=size:raise RuntimeError('incomplete model download')
    if offset!=size or digest.hexdigest()!=item['sha256']:
        raise RuntimeError('model file checksum mismatch: '+item['path'])


def layer(items,local_root,prefix):
    paths=[info(prefix.rsplit('/',1)[0]+'/',directory=True),info(prefix+'/',directory=True)]
    paths += [info(prefix+'/'+r['path'],r['size']) for r in items]
    total=sum(512+((p.size+511)//512)*512 for p in paths)+1024
    total=((total+10239)//10240)*10240
    digest=hashlib.sha256()
    def generate():
        written=0
        for p in paths:
            # GNU base-256 size fields support the single 24GB safetensors file.
            # Plain USTAR would overflow its 8GiB member-size field.
            data=p.tobuf(format=tarfile.GNU_FORMAT);digest.update(data);written+=len(data);yield data
            if p.size:
                item=next(r for r in items if prefix+'/'+r['path']==p.name)
                local=local_root/item['path'] if not item.get('remote_stream') else None
                for data in chunks(item,local):digest.update(data);written+=len(data);yield data
                padding=(-p.size)%512
                if padding:
                    data=b'\0'*padding;digest.update(data);written+=len(data);yield data
        data=b'\0'*(total-written);digest.update(data);yield data
    return total,generate(),digest


def export(source,tag,items,local_root,output):
    proc=subprocess.Popen([*PREFIX,'docker','-H',SOCKET,'save',source],stdout=subprocess.PIPE)
    small={};manifest=None;raw_size=0
    with tarfile.open(fileobj=output,mode='w|') as dest:
        with tarfile.open(fileobj=proc.stdout,mode='r|') as src:
            for entry in src:
                data=src.extractfile(entry) if entry.isfile() else None
                if entry.name in ('manifest.json','index.json','oci-layout','repositories'):
                    value=data.read() if data else b''
                    if entry.name=='manifest.json':manifest=json.loads(value)
                    continue
                if data and entry.size<1024*1024:
                    content=data.read();small[entry.name]=content;data=io.BytesIO(content)
                dest.addfile(entry,data)
        if proc.wait()!=0:raise RuntimeError('docker save failed')
        if not manifest or len(manifest)!=1:raise RuntimeError('expected one runtime image')
        original=manifest[0]
        config=json.loads(small[original['Config']])
        if config.get('architecture')!='amd64' or config.get('os')!='linux':
            raise RuntimeError('source runtime image is not linux/amd64')
        size,body,digest=layer(items,local_root,'code/models/gemma-4-12B-it')
        layer_name=hashlib.sha256(json.dumps(items,sort_keys=True).encode()).hexdigest()+'/layer.tar'
        dest.addfile(info(layer_name,size),Reader(body))
        diff_id='sha256:'+digest.hexdigest()
        config['rootfs']['diff_ids'].append(diff_id)
        config.setdefault('history',[]).append({'created_by':'COPY fixed-revision inference assets; SHA256 verified while exporting'})
        config_bytes=json.dumps(config,separators=(',',':')).encode()
        config_name=hashlib.sha256(config_bytes).hexdigest()+'.json'
        dest.addfile(info(config_name,len(config_bytes)),io.BytesIO(config_bytes))
        final=[{'Config':config_name,'RepoTags':[tag],'Layers':[*original['Layers'],layer_name]}]
        value=json.dumps(final,separators=(',',':')).encode()
        dest.addfile(info('manifest.json',len(value)),io.BytesIO(value))
        raw_size=dest.offset
    # tarfile pads its final two zero blocks to RECORDSIZE.
    raw_size=((raw_size+1024+10239)//10240)*10240
    return {'uncompressed_tar_bytes':raw_size,'model_layer_diff_id':diff_id,
            'image_tag':tag,'platform':'linux/amd64','model_files_verified':len(items)}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',required=True);p.add_argument('--tag',required=True)
    p.add_argument('--model-manifest',type=Path,required=True)
    p.add_argument('--model-files',type=Path,required=True)
    p.add_argument('--output',type=Path)
    p.add_argument('--remote')
    p.add_argument('--receipt',type=Path,required=True)
    args=p.parse_args()
    items=json.loads(args.model_manifest.read_text())['files']
    if bool(args.output)==bool(args.remote):raise ValueError('select local output or remote stream')
    if args.remote:
        remote=args.remote+'.incomplete'
        upload=subprocess.Popen(['rclone','rcat',remote,'--drive-chunk-size','64M','--buffer-size','8M'],stdin=subprocess.PIPE)
        stream=upload.stdin
    else:stream=args.output.open('xb')
    writer=CountingWriter(stream)
    compressor=subprocess.Popen(['pigz','-1','-p','8','-n','-c'],stdin=subprocess.PIPE,stdout=subprocess.PIPE)
    errors=[]
    def pump():
        try:
            while data:=compressor.stdout.read(1024*1024):writer.write(data)
        except BaseException as exc:
            errors.append(exc);compressor.terminate()
    thread=threading.Thread(target=pump,daemon=True);thread.start()
    try:
        record=export(args.source,args.tag,items,args.model_files,compressor.stdin)
        compressor.stdin.close()
        if compressor.wait()!=0:raise RuntimeError('parallel compression failed')
        thread.join()
        if errors:raise errors[0]
        writer.flush();stream.close()
        if args.remote and upload.wait()!=0:raise RuntimeError('Drive upload failed')
        record.update(md5=writer.hash.hexdigest(),compressed_bytes=writer.bytes,
                      exported_utc=datetime.datetime.now(datetime.timezone.utc).isoformat())
        if record['uncompressed_tar_bytes']>100_000_000_000:raise RuntimeError('image exceeds guide size limit')
        if args.remote:
            remote_md5=subprocess.check_output(['rclone','md5sum',remote],text=True).split()[0]
            if remote_md5!=record['md5']:raise RuntimeError('uploaded archive MD5 mismatch')
            subprocess.run(['rclone','moveto',remote,args.remote],check=True)
            record['remote']=args.remote
        args.receipt.write_text(json.dumps(record,indent=2)+'\n')
        print(json.dumps(record),flush=True)
    except BaseException:
        compressor.terminate()
        compressor.wait()
        if args.remote:
            upload.terminate();upload.wait()
        raise


if __name__=='__main__':main()
