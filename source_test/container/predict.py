#!/usr/bin/env python3
"""Fixed offline entry point for the official /data -> /result container contract."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path('/code')
MODEL = ROOT/'models/gemma-4-12B-it'
PREFLIGHT_TIMEOUT_SECONDS = 600
FP16_MIN_TOTAL_GB = 26.0  # 12B parameters in fp16 are about 24 GB, plus activations and cache
MARK = 'PREFLIGHT_JSON '


def say(message):
    print('[predict ' + time.strftime('%H:%M:%S') + '] ' + message, flush=True)


def parse_preflight(stdout):
    """The report printed by gpu_preflight.py, or None when it printed none."""
    for line in reversed((stdout or '').splitlines()):
        if line.startswith(MARK):
            try:
                return json.loads(line[len(MARK):])
            except ValueError:
                return None
    return None


def decide_quant(report):
    """(DATAMATE_QUANT value or None, problems, notes) from a preflight report.
    problems are reasons the run cannot work at all (raised); notes explain a choice. An NF4 failure on any GPU selects
    unquantized fp16 for every worker, since a worker that fails in NF4 would otherwise fall back on its own."""
    problems, notes = [], []
    if not report:
        return None, ['the GPU preflight printed no report'], notes
    if report.get('torch_error'):
        problems.append('torch failed to load: ' + report['torch_error'])
    elif not report.get('cuda_available'):
        problems.append('torch reports no CUDA device')
    gpus = report.get('gpus') or []
    if not problems and len(gpus) < 2:
        problems.append(f'two CUDA GPUs are required, found {len(gpus)}')
    for gpu in gpus:
        if not gpu.get('torch_has_arch'):
            notes.append(f"torch {report.get('torch')} lists no kernels for GPU {gpu['index']} "
                         f"(capability {gpu['capability']}); arch list: {report.get('arch_list')}")
    nf4_ok = report.get('nf4_ok') or {}
    broken = [index for index, ok in nf4_ok.items() if not ok]
    if report.get('bitsandbytes_error') or broken or (not nf4_ok and gpus):
        reason = report.get('bitsandbytes_error') or json.dumps(report.get('nf4_error') or 'no NF4 result')
        notes.append('NF4 is unusable (' + reason + '); weights will be loaded as unquantized fp16')
        small = [g for g in gpus if g.get('total_gb', 0) < FP16_MIN_TOTAL_GB]
        if small:
            notes.append(f'WARNING: fp16 needs about {FP16_MIN_TOTAL_GB:.0f} GB per GPU; smaller: '
                         + ', '.join(f"GPU {g['index']} {g['total_gb']} GB" for g in small))
        return 'fp16', problems, notes
    return None, problems, notes


def run_preflight(result):
    env = dict(os.environ)
    env.pop('CUDA_VISIBLE_DEVICES', None)
    completed = subprocess.run([sys.executable, str(ROOT/'container/gpu_preflight.py')], capture_output=True, text=True,
                               timeout=PREFLIGHT_TIMEOUT_SECONDS, env=env, check=False)
    report = parse_preflight(completed.stdout)
    (result/'logs/environment.json').write_text(json.dumps(report, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')
    if completed.stderr.strip():
        (result/'logs/preflight_stderr.txt').write_text(completed.stderr[-20000:], encoding='utf-8')
    say('GPU preflight: ' + json.dumps(report, ensure_ascii=False))
    if report is None and completed.stderr.strip():
        say('preflight stderr tail: ' + completed.stderr[-1500:].replace('\n', ' | '))
    return report


def main():
    started = time.monotonic()
    result = Path('/result'); (result/'logs').mkdir(parents=True,exist_ok=True)
    if not Path('/data/questions.jsonl').is_file():
        raise RuntimeError('/data/questions.jsonl is missing')
    metadata=json.loads((ROOT/'model_manifest.json').read_text())
    for item in metadata['files']:
        path=MODEL/item['path']
        if not path.is_file() or path.stat().st_size!=item['size']:
            raise RuntimeError('inference model file missing or wrong size: '+item['path'])
    report = run_preflight(result)
    quant, problems, notes = decide_quant(report)
    for note in notes:
        say(note)
    if problems:
        raise RuntimeError('; '.join(problems) + '; no CPU or external-API fallback')
    if quant:
        os.environ['DATAMATE_QUANT'] = quant
        say('DATAMATE_QUANT=' + quant)
    argv=[sys.executable,str(ROOT/'src/run_submission.py'),'--data','/data',
          '--model-dir',str(MODEL),'--out','/result','--submission','/result/submission.jsonl',
          '--fewshot-bank',str(ROOT/'assets/fewshot_bank.jsonl'),'--total-seconds','21000',
          '--workers','4','--gpus','0,1','--batch-max','1','--traces']
    completed=subprocess.run(argv,cwd=ROOT/'src',check=False)
    # run_submission handles interruption/checkpoint recovery. Supply transparent logs
    # for questions that never reached SolverPool (e.g. a server startup failure).
    seen=set()
    path=result/'logs/questions.jsonl'
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():seen.add(json.loads(line)['id'])
    submission=result/'submission.jsonl'
    if submission.exists():
        with path.open('a',encoding='utf-8') as out:
            for line in submission.read_text().splitlines():
                if not line.strip():continue
                row=json.loads(line)
                if row['id'] in seen:continue
                record={'id':row['id'],'answer':{k:v for k,v in row.items() if k!='id'},
                        'elapsed_ms':max(0,round((time.monotonic()-started)*1000)),
                        'thinking':None,'code':'/code/src/run_submission.py::rebuild_final',
                        'inputs':{'out':'/result','data':'/data'},'output':row,
                        'output_source':'checkpoint recovery; question was not solved',
                        'infrastructure_or_budget_recovery':True}
                out.write(json.dumps(record,ensure_ascii=False)+'\n');out.flush()
    return completed.returncode


if __name__=='__main__':
    try:sys.exit(main())
    except Exception as exc:
        # Configuration failures are visible failures, not successful all-abstain trials.
        print('CONTAINER PREFLIGHT FAILED: '+type(exc).__name__+': '+str(exc),file=sys.stderr)
        sys.exit(2)
