"""Group categorical value variants for every business with the wave-eight annotator."""
import collections, glob, json, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
D = Path("/home/ubuntu/datamate_review_data"); MAIN = Path("/home/ubuntu/datamate/source_test")
sys.path.insert(0, "/home/ubuntu/agy-worktrees/arch8/source_test")
from bizreason.config import Settings
from bizreason.contracts import BusinessContext
from bizreason.llm import TrammayClient
from bizreason.semantic.annotator import Annotator, load_value_groups
cfg = json.load(open(MAIN / "artifacts/kaggle_12h_server/connection_config.json")); ep = json.load(open(MAIN / "artifacts/kaggle_12h_server/active_endpoint.json"))
ann = Annotator(TrammayClient(Settings(base_url=ep["base_url"], model=cfg["model"], api_key=cfg["bearer_token"], max_output_tokens=2000, request_timeout=280)), workers=1)
notes = collections.defaultdict(list)
for f in ["spike_a1b", "ann_rest", "ann_fix", "ann_fix2"]:
    for line in (D / f / "annotations.jsonl").open(encoding="utf-8"):
        r = json.loads(line); notes[r["biz"]].append(r)
(D / "value_groups").mkdir(exist_ok=True)
def run(biz):
    p = Path(sorted(glob.glob(str(MAIN / f"artifacts/cache/{biz}/*/catalog.json")))[-1]); d = json.load(open(p)); d["database"] = str(p.with_name("business.sqlite"))
    g = load_value_groups(ann, BusinessContext.model_validate(d), notes[biz], D / "value_groups" / f"{biz}.cache.json")
    (D / "value_groups" / f"{biz}.json").write_text(json.dumps(g, ensure_ascii=False), encoding="utf-8")
    return biz, {k: len(v) for k, v in g.items()}
with ThreadPoolExecutor(4) as pool:
    for biz, n in pool.map(run, sorted(notes)):
        print("groups", biz, n, flush=True)
print("done", flush=True)
