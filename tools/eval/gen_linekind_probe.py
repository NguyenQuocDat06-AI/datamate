"""Probe: are line-kind classifications stable across two calls on the same server? Writes linekind_probe/<biz>_<n>.json."""
import json, sys
sys.path.insert(0, "/home/ubuntu/datamate_review_data")
import alias_off8 as A
from pathlib import Path

def main(biz):
    cfg = json.load(open(A.MAIN / "artifacts/kaggle_12h_server/connection_config.json"))
    ep = json.load(open(A.MAIN / "artifacts/kaggle_12h_server/active_endpoint.json"))
    client = A.TrammayClient(A.Settings(base_url=ep["base_url"], model=cfg["model"], api_key=cfg["bearer_token"], max_output_tokens=1500, request_timeout=280))
    annotator = A.Annotator(client, workers=4)
    builder, _, _ = A._builder(A.DATA, A.OUT / "cache")
    context = builder.prepare(A.DATA / "data" / biz)
    records = A.load_or_annotate(annotator, context, A.OUT / "ann" / f"{biz}.json")
    cat, sold = A.names(context, records, {"product_catalog"}), A.names(context, records, {"sales"})
    out = Path("/home/ubuntu/datamate_review_data/linekind_probe"); out.mkdir(exist_ok=True)
    runs = []
    for n in range(2):
        kinds = annotator.annotate_line_kinds(context, records, cat, sold)
        (out / f"{biz}_{n}.json").write_text(json.dumps(kinds, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        runs.append(kinds)
    diff = [k for k in set(runs[0]) | set(runs[1]) if runs[0].get(k) != runs[1].get(k)]
    print(biz, "catalog", len(cat), "sold", len(sold), "classified", len(runs[0]), len(runs[1]), "differ", len(diff), diff[:8])

if __name__ == "__main__":
    main(sys.argv[1])
