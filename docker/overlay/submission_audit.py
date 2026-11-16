"""Submission-only observability. Does not select, repair or change an answer."""
from __future__ import annotations

import contextvars
import json
import os
from pathlib import Path
import re
import threading
import time

_CURRENT = contextvars.ContextVar('submission_question', default=None)
_LOCK = threading.Lock()
_QUEUED = None


def generated_thinking(text):
    """Keep generated reasoning verbatim; empty thought channels mean no reasoning."""
    if not isinstance(text, str):
        return None
    patterns = (r'<think>([\s\S]*?)(?:</think>|$)',
                r'<\|channel>thought\n([\s\S]*?)(?:<channel\|>|$)',
                r'<\|thought>([\s\S]*?)(?:<\|turn>|$)')
    found = []
    for pattern in patterns:
        found.extend((m.start(), m.group(1)) for m in re.finditer(pattern, text, re.I))
    found.sort()
    value = ''.join(value for _, value in found)
    return value if value.strip() else None


def observe_payload(payload):
    record = _CURRENT.get()
    if record is None or not isinstance(payload, dict):
        return
    choices = payload.get('choices') or []
    if not choices:
        return
    message = choices[0].get('message') or {}
    thought = message.get('reasoning_content')
    if not isinstance(thought, str):
        thought = generated_thinking(message.get('content'))
    record['calls'].append(thought if isinstance(thought, str) else '')


def _append(record):
    folder = Path(os.environ['DATAMATE_AUDIT_LOG_DIR'])
    folder.mkdir(parents=True, exist_ok=True)
    with _LOCK, (folder/'questions.jsonl').open('a',encoding='utf-8') as handle:
        handle.write(json.dumps(record,ensure_ascii=False,allow_nan=False)+'\n')
        handle.flush()


def install(module):
    """Wrap real call boundaries in the __main__ pipeline module exactly once."""
    if getattr(module, '_SUBMISSION_AUDIT_INSTALLED', False):
        return
    module._SUBMISSION_AUDIT_INSTALLED = True
    original_build = module.build_and_answer
    def build(*args, **kwargs):
        global _QUEUED
        _QUEUED = time.monotonic()
        return original_build(*args, **kwargs)
    module.build_and_answer = build

    original_request = module.TrammayClient._request
    def request(self, *args, **kwargs):
        payload = original_request(self,*args,**kwargs)
        observe_payload(payload)
        return payload
    module.TrammayClient._request = request

    original_solve = module.SolverPool.solve
    def solve(self, question, deadline):
        started = time.monotonic()
        current = {'calls':[]}
        context_token = _CURRENT.set(current)
        try:
            outcome = original_solve(self, question, deadline)
            prediction = json.loads(outcome.line)
            calls = current['calls']
            thinking = (calls[0] if len(calls)==1 else calls) if any(v.strip() for v in calls) else None
            _append({'id':question.id,
                     'answer':{k:v for k,v in prediction.items() if k!='id'},
                     'elapsed_ms':max(0,round((outcome.finished-(_QUEUED or started))*1000)),
                     'solver_elapsed_ms':max(0,round(outcome.seconds*1000)),
                     'thinking':thinking,
                     'code':'/code/src/bizreason/run_v2.py::SolverPool.solve',
                     'inputs':{'question':question.model_dump(mode='json'),'deadline':deadline,
                               'business_database':str(self._paths.get(question.business_id,''))},
                     'output':prediction,
                     'output_source':'Outcome.line, decoded from the actual function return',
                     'model_calls':len(calls)})
            return outcome
        finally:
            _CURRENT.reset(context_token)
    module.SolverPool.solve = solve
