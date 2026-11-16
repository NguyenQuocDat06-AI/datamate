"""Validate organizer TRAIN provenance and reproduce the frozen example-bank bytes."""
import argparse
import hashlib
import json
from pathlib import Path

root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--out',type=Path,required=True)
parser.add_argument('--reference',type=Path)
args=parser.parse_args()
questions={row['id']:row for row in (json.loads(x) for x in (root/'data/train/questions.jsonl').read_text().splitlines() if x.strip())}
source=root/'data/precomputed_intents.jsonl'
for row in (json.loads(x) for x in source.read_text().splitlines() if x.strip()):
    original=questions.get(row['id'])
    if original is None or any(row[key]!=original[key] for key in ('question','business_id')):
        raise ValueError('example is not grounded in organizer TRAIN')
    if not isinstance(row['intent'],dict):
        raise ValueError('intent must be a structured object')
args.out.parent.mkdir(parents=True,exist_ok=True)
args.out.write_bytes(source.read_bytes())
if args.reference is not None and args.out.read_bytes()!=args.reference.read_bytes():
    raise ValueError('reproduced bank differs from the reference bytes')
print('export_sha256',hashlib.sha256(source.read_bytes()).hexdigest())
