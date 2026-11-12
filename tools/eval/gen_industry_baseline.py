#!/usr/bin/env python3
"""Per-industry PA baseline for measurement 4 (leave-one-industry-out).

Reads (read-only):
  - train rulebook and public rulebook (industry sections under "## 6. Theo ngành")
  - shop READMEs + data files (only for keyword evidence of the industry mapping)
  - train_full_2/records.jsonl (fallback: train_full_root/questions.jsonl for biz)

Writes:
  - /home/ubuntu/datamate_review_data/industry_baseline.json
Prints a table to stdout.

PA = (correct - wrong) / n, n = all questions of the group (abstain counts in n only).
Bootstrap 95% CI over shops: 2000 resamples of shops with replacement, seed 0,
only for groups with >= 3 shops.
"""
import collections
import json
import os
import random
import re
import unicodedata
import zipfile
import html as htmllib

ROOT = "/home/ubuntu/datamate_review_data"
TRAIN_RULEBOOK = "/home/ubuntu/datamate/source_test/data/train/rulebook.md"
PUBLIC_RULEBOOK = os.path.join(ROOT, "rulebook_public.md")
SHOP_DIR = "/home/ubuntu/datamate/source_test/data/train/data"
RECORDS = os.path.join(ROOT, "train_full_2", "records.jsonl")
QUESTIONS = os.path.join(ROOT, "train_full_root", "questions.jsonl")
OUT = os.path.join(ROOT, "industry_baseline.json")

BOOT_N = 2000
BOOT_SEED = 0
MIN_SHOPS_CI = 3

NONE_GROUP = "(khong thuoc nganh trong rulebook)"
UNKNOWN_GROUP = "(khong xac dinh duoc)"

# Shop -> industry mapping. Value is the rulebook heading text, or NONE_GROUP / UNKNOWN_GROUP.
# Evidence is generated per shop from the README and data (see shop_evidence()).
MAP = {}
for b in ["104", "105", "106", "111", "112"]:
    MAP["biz_" + b] = "Cà phê"
for b in ["119", "120", "121", "122", "123", "124"]:
    MAP["biz_" + b] = "Nhà thuốc"
for b in ["113", "114", "115", "116", "117", "118"]:
    MAP["biz_" + b] = "Vật liệu xây dựng"
# General grocery / F&B shops with no industry section in either rulebook.
for b in ["101", "102", "103", "107", "108", "109", "125", "126", "127", "128", "129", "130"]:
    MAP["biz_" + b] = NONE_GROUP
# No product or item information in data (bill-level CSV only, README has no product types).
MAP["biz_110"] = UNKNOWN_GROUP

NOTE = {
    NONE_GROUP: "general grocery or food/drink shop (products such as Mì, Trứng, Coca Cola, Bia Tiger, Thuốc lá, Tã/Sữa mixed in); no rulebook industry matches",
    UNKNOWN_GROUP: "README and data show only bill totals (Tien hang, TongTien, Bên mua); no product type to match an industry",
}

# Keyword evidence (accent-stripped, lowercase). Used only to print evidence per shop.
KW = {
    "Cà phê": ["(m)", "(l)", "ca phe", "tra sua"],
    "Nhà thuốc": ["panadol", "ke don", "hoat chat"],
    "Vật liệu xây dựng": ["xi mang", "gach", "bien so"],
    "Mẹ và bé": ["bim", "size bim"],
    "Sửa xe": ["nhot", "luot xe", "phieu bao hanh"],
    "Cửa hàng điện thoại": ["iphone", "samsung", "dong may"],
}


def strip_accents(s):
    s = s.replace("đ", "d").replace("Đ", "D")
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).lower().strip()


def parse_industries(path):
    """Return {normalized_name: {"name": heading, "rows": [rule names]}} from section 6."""
    lines = open(path, encoding="utf8").read().splitlines()
    start = next(i for i, l in enumerate(lines) if re.match(r"^## 6\.", l))
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## ") and not lines[j].startswith("### "):
            end = j
            break
    out = {}
    cur = None
    for l in lines[start:end]:
        if l.startswith("### "):
            cur = l[4:].strip()
            out[strip_accents(cur)] = {"name": cur, "rows": []}
            continue
        if cur and l.startswith("|"):
            if re.match(r"^\|[\s\-|:]+\|$", l):
                continue
            cells = [c.strip() for c in l.strip().strip("|").split("|")]
            first = cells[0].replace("**", "")
            if first in ("Luật",):  # header row
                continue
            out[strip_accents(cur)]["rows"].append(first)
    return out


def scan_text(path):
    low = path.lower()
    if low.endswith(".xlsx"):
        z = zipfile.ZipFile(path)
        for n in z.namelist():
            if n.startswith("xl/") and n.endswith(".xml"):
                x = z.read(n).decode("utf8", "ignore")
                yield " ".join(htmllib.unescape(m) for m in re.findall(r">([^<]+)<", x))
    elif low.endswith(".csv") or low.endswith(".sqlite"):
        yield open(path, encoding="utf8", errors="ignore").read()


def shop_evidence(biz, industry):
    d = os.path.join(SHOP_DIR, biz)
    hits = collections.Counter()
    for f in sorted(os.listdir(d)):
        p = os.path.join(d, f)
        if f == "README.md":
            continue
        for t in scan_text(p):
            tl = strip_accents(t)
            for k in KW.get(industry, []):
                c = tl.count(k)
                if c:
                    hits[k] += c
    if industry in NOTE:
        basis = NOTE[industry]
    else:
        basis = "keyword hits in data: " + (", ".join(f"{k}={v}" for k, v in hits.most_common(4)) or "none")
    return basis


def load_records():
    rows = []
    qmap = {}
    if os.path.exists(QUESTIONS):
        for l in open(QUESTIONS, encoding="utf8"):
            if l.strip():
                q = json.loads(l)
                qmap[q["id"]] = q.get("business_id")
    for l in open(RECORDS, encoding="utf8"):
        if not l.strip():
            continue
        r = json.loads(l)
        biz = r.get("biz") or qmap.get(r["id"])
        rows.append({"id": r["id"], "biz": biz, "outcome": r.get("outcome")})
    return rows


def pa_of(c, w, n):
    return (c - w) / n if n else None


def bootstrap_ci(shop_stats):
    """shop_stats: list of (correct, wrong, n). Resample shops with replacement."""
    if len(shop_stats) < MIN_SHOPS_CI:
        return None
    rng = random.Random(BOOT_SEED)
    k = len(shop_stats)
    vals = []
    for _ in range(BOOT_N):
        c = w = n = 0
        for _ in range(k):
            sc, sw, sn = shop_stats[rng.randrange(k)]
            c += sc
            w += sw
            n += sn
        if n:
            vals.append((c - w) / n)
    vals.sort()
    lo = vals[int(0.025 * len(vals))]
    hi = vals[min(len(vals) - 1, int(0.975 * len(vals)) - 1)]
    return [round(lo, 4), round(hi, 4)]


def main():
    train_ind = parse_industries(TRAIN_RULEBOOK)
    public_ind = parse_industries(PUBLIC_RULEBOOK)
    all_names = {}
    for src in (train_ind, public_ind):
        for k, v in src.items():
            all_names.setdefault(k, v["name"])
    for biz, ind in MAP.items():
        if ind not in (NONE_GROUP, UNKNOWN_GROUP):
            assert strip_accents(ind) in all_names, f"{biz}: {ind!r} not a rulebook industry"

    records = load_records()
    per_shop = collections.defaultdict(lambda: collections.Counter())
    for r in records:
        per_shop[r["biz"]]["n"] += 1
        if r["outcome"] == "correct":
            per_shop[r["biz"]]["c"] += 1
        elif r["outcome"] == "wrong":
            per_shop[r["biz"]]["w"] += 1
        else:
            per_shop[r["biz"]]["a"] += 1

    shops_all = sorted(set(per_shop) | {b for b in os.listdir(SHOP_DIR) if b.startswith("biz_")})
    unmapped = [b for b in shops_all if b not in MAP]
    assert not unmapped, f"shops without mapping: {unmapped}"

    groups = collections.OrderedDict()
    order = ["Cà phê", "Nhà thuốc", "Vật liệu xây dựng", "Mẹ và bé", "Sửa xe", "Cửa hàng điện thoại",
             NONE_GROUP, UNKNOWN_GROUP]
    for g in order:
        groups[g] = []
    for biz in shops_all:
        groups[MAP[biz]].append(biz)

    shop_rows = []
    for biz in shops_all:
        st = per_shop.get(biz, collections.Counter())
        ind = MAP[biz]
        shop_rows.append({
            "biz": biz,
            "industry": ind,
            "evidence": shop_evidence(biz, ind),
            "questions": st["n"], "correct": st["c"], "wrong": st["w"], "abstain": st["a"],
        })

    result_groups = []
    for g, shops in groups.items():
        c = w = n = a = 0
        stats = []
        for biz in shops:
            st = per_shop.get(biz, collections.Counter())
            c += st["c"]; w += st["w"]; n += st["n"]; a += st["a"]
            stats.append((st["c"], st["w"], st["n"]))
        rulebook_group = g not in (NONE_GROUP, UNKNOWN_GROUP)
        tr = train_ind.get(strip_accents(g)) if rulebook_group else None
        pu = public_ind.get(strip_accents(g)) if rulebook_group else None
        result_groups.append({
            "industry": g,
            "shops": shops,
            "n_shops": len(shops),
            "questions": n,
            "correct": c,
            "wrong": w,
            "abstain": a,
            "pa": round(pa_of(c, w, n), 4) if n else None,
            "pa_ci95": bootstrap_ci(stats),
            # None = industry section absent from that rulebook; 0 is not used for "absent".
            "rulebook_rows_train": (len(tr["rows"]) if tr else None) if rulebook_group else 0,
            "rulebook_rule_names_train": tr["rows"] if tr else [],
            "rulebook_rows_public": len(pu["rows"]) if pu else None,
            "rulebook_rule_names_public": pu["rows"] if pu else [],
        })

    total = {"questions": len(records),
             "correct": sum(1 for r in records if r["outcome"] == "correct"),
             "wrong": sum(1 for r in records if r["outcome"] == "wrong"),
             "abstain": sum(1 for r in records if r["outcome"] not in ("correct", "wrong"))}
    total["pa"] = round(pa_of(total["correct"], total["wrong"], total["questions"]), 4)

    out = {
        "source_run": "train_full_2",
        "rulebook_for_run": "train rulebook (train_full_root/rulebook.md)",
        "bootstrap": {"resamples": BOOT_N, "seed": BOOT_SEED, "min_shops": MIN_SHOPS_CI, "unit": "shop"},
        "total": total,
        "groups": result_groups,
        "shops": shop_rows,
    }
    with open(OUT, "w", encoding="utf8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"{'industry':<26}{'shops':>6}{'n':>6}{'corr':>6}{'wrong':>6}{'abst':>6}{'PA':>9}  {'CI95':<18}{'rules tr/pub':>13}")
    for g in result_groups:
        ci = "-" if g["pa_ci95"] is None else f"[{g['pa_ci95'][0]:+.3f}, {g['pa_ci95'][1]:+.3f}]"
        pa = "-" if g["pa"] is None else f"{g['pa']:+.4f}"
        tr = "-" if g["rulebook_rows_train"] is None else str(g["rulebook_rows_train"])
        pu = "-" if g["rulebook_rows_public"] is None else str(g["rulebook_rows_public"])
        print(f"{g['industry'][:25]:<26}{g['n_shops']:>6}{g['questions']:>6}{g['correct']:>6}{g['wrong']:>6}"
              f"{g['abstain']:>6}{pa:>9}  {ci:<18}{tr + '/' + pu:>13}")
    print(f"{'TOTAL':<26}{len(shops_all):>6}{total['questions']:>6}{total['correct']:>6}{total['wrong']:>6}"
          f"{total['abstain']:>6}{total['pa']:>+9.4f}")
    print()
    print("shop -> industry")
    for s in shop_rows:
        print(f"  {s['biz']:<8} {s['industry'][:28]:<29} n={s['questions']:<4} | {s['evidence'][:110]}")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
