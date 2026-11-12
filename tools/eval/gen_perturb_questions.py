#!/usr/bin/env python3
"""Generate meaning-preserving question variants for Measurement 2.

For a stratified sample of questions, emit the original line plus up to three
variants per question:

  strip    remove Vietnamese diacritics (d-stroke mapped to d)
  abbrev   replace common shop-owner shorthand; filler " vậy" if no rule applies
  reorder  move a leading time expression to the end (or a trailing one to the front)

Variant ids are f"{orig_id}__{kind}". variants.json maps variant id -> original id.
Deterministic, code only: no network, no model calls.

Usage:
  python gen_perturb_questions.py --questions IN.jsonl --out OUT_DIR --sample 500 --seed 0
"""
import argparse
import json
import random
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

# Shop-owner shorthand. Keys are matched case-insensitively, either exactly as
# written (with diacritics) or fully unaccented (e.g. "bao nhieu"); a mix of the
# two is not matched, so "tiến" never turns into "tien".
ABBREVIATIONS = {
    "bao nhiêu": "bnhiu",
    "doanh thu": "dthu",
    "hôm nay": "hnay",
    "hôm qua": "hqua",
    "được": "dc",
    "không": "k",
    "tháng": "th",  # only when directly followed by a space+digit or a digit
    "đơn": "don",
    "khách": "kh",
    "tiền": "tien",
}
# Keys that only abbreviate when a digit follows (e.g. "tháng 3/2025" -> "th 3/2025").
ABBREV_DIGIT_CONTEXT = {"tháng"}
FILLER = " vậy"
KINDS = ("strip", "abbrev", "reorder")

# Time expressions, matched on the diacritic-free lowercase copy of the question.
_TIME_ALTS = [
    r"tu\s+(?:thang|ngay|tuan|nam|quy|hom)\b[^,?.!]{0,40}?\s+den\s+(?:thang|ngay|tuan|nam|quy|hom)\b[^,?.!]{0,40}?(?=[,?.!]|$)",
    r"30\s+ngay\s+gan\s+nhat",
    r"ngay\s+\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{4}",
    r"\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{4}",
    r"thang\s+\d{1,2}\s*/\s*\d{4}",
    r"th\d{1,2}\s*/\s*\d{4}",
    r"t\d{1,2}\s*/\s*\d{4}",
    r"\d{1,2}\s*/\s*\d{4}",
    r"\d{1,2}-\d{4}",
    r"(?:hom\s+nay|hnay)",
    r"(?:hom\s+qua|hqua)",
    r"tuan\s+truoc",
    r"thang\s+nay",
    r"thang\s+truoc",
    r"nam\s+nay",
]
_TIME_RE = re.compile(
    r"(?<![\w/])(?:(?:trong|vao|o)\s+)?(?:"
    + "|".join(f"(?:{a})" for a in _TIME_ALTS)
    + r")(?![\w/])"
)


def _fold_char(ch):
    """Diacritic-free form of one character (may be empty for a bare combining mark)."""
    if ch == "đ":
        return "d"
    if ch == "Đ":
        return "D"
    return "".join(c for c in unicodedata.normalize("NFD", ch) if unicodedata.category(c) != "Mn")


def strip_diacritics(text):
    return "".join(_fold_char(c) for c in unicodedata.normalize("NFC", text))


def _fold_lower_map(text):
    """Return (folded lowercase text, map stripped-index -> original-index, length list end)."""
    pieces = []
    s2o = []
    for i, ch in enumerate(text):
        piece = _fold_char(ch).lower()
        s2o.extend([i] * len(piece))
        pieces.append(piece)
    s2o.append(len(text))
    return "".join(pieces), s2o


def _split_term(s):
    """Split trailing sentence punctuation off: 'a b?' -> ('a b', '?')."""
    s = s.rstrip()
    term = re.search(r"[?.!]*$", s).group(0)
    return s[: len(s) - len(term)].rstrip(), term


def _abbrev_regex(phrase, digit_context):
    phrase = unicodedata.normalize("NFC", phrase)
    exact = r"\s+".join(re.escape(w) for w in phrase.split())
    plain = r"\s+".join(re.escape(w) for w in strip_diacritics(phrase).split())
    alt = exact if exact == plain else f"(?:{exact}|{plain})"
    suffix = r"(?=\s\d|\d)" if digit_context else r"(?!\w)"
    return re.compile(rf"(?<!\w){alt}{suffix}", re.IGNORECASE)


_ABBR_RULES = [
    (_abbrev_regex(k, k in ABBREV_DIGIT_CONTEXT), v)
    for k, v in sorted(ABBREVIATIONS.items(), key=lambda kv: len(kv[0]), reverse=True)
]


def make_strip(question):
    return strip_diacritics(question)


def make_abbrev(question):
    out = question
    applied = 0
    for rx, rep in _ABBR_RULES:
        out, n = rx.subn(rep, out)
        applied += n
    if applied == 0 or out == question:
        core, term = _split_term(question)
        return f"{core}{FILLER}{term}"
    return out


def find_time(question):
    """Return (start, end, position) of the single time expression, or (None, reason)."""
    folded, s2o = _fold_lower_map(question)
    matches = list(_TIME_RE.finditer(folded))
    if not matches:
        return None, "no_time"
    if len(matches) > 1:
        return None, "multiple_time"
    m = matches[0]
    a = s2o[m.start()]
    b = s2o[m.end()]
    if question[:a].strip() == "":
        return (a, b, "start"), None
    if re.fullmatch(r"[\s?.!]*", question[b:]):
        return (a, b, "end"), None
    return None, "middle"


def make_reorder(question):
    """Return (variant or None, reason or None)."""
    span, reason = find_time(question)
    if span is None:
        return None, reason
    a, b, position = span
    time_txt = question[a:b].strip()
    if position == "start":
        # Lowercase a Title-case opener now that it is no longer sentence-initial.
        # All-caps/code forms such as "T6/2025" keep their case.
        if len(time_txt) > 1 and time_txt[0].isupper() and time_txt[1].islower():
            time_txt = time_txt[0].lower() + time_txt[1:]
        body, term = _split_term(question[b:].lstrip(" ,;:"))
        if not body:
            return None, "empty"
        new = f"{body} {time_txt}{term}"
    else:
        core = question[:a].rstrip(" ,;:")
        if not core.strip():
            return None, "empty"
        term = question[b:].strip()
        new = f"{time_txt} {core}{term}"
    new = new.strip()
    if new == question:
        return None, "same"
    return new, None


def make_variants(question):
    """Return {kind: (text or None, skip reason or None)} for one question."""
    question = unicodedata.normalize("NFC", question)
    results = {}
    s = make_strip(question)
    results["strip"] = (s, None) if s != question else (None, "noop")
    a = make_abbrev(question)
    results["abbrev"] = (a, None) if a != question else (None, "noop")
    results["reorder"] = make_reorder(question)
    return results


def allocate(sizes, n):
    """Equal share per group, capped by group size; leftover goes to larger groups."""
    keys = sorted(sizes, key=lambda k: (sizes[k], k))
    remaining = n
    alloc = {}
    for i, k in enumerate(keys):
        take = min(sizes[k], remaining // (len(keys) - i))
        alloc[k] = take
        remaining -= take
    return alloc


def sample_indices(records, n, seed):
    """Stratified-by-business_id sample; returns record indices in file order."""
    groups = {}
    for idx, rec in enumerate(records):
        groups.setdefault(rec["business_id"], []).append(idx)
    sizes = {k: len(v) for k, v in groups.items()}
    alloc = allocate(sizes, n)
    rng = random.Random(seed)
    chosen = []
    for k in sorted(groups):
        pool = sorted(groups[k], key=lambda i: records[i]["id"])
        chosen.extend(rng.sample(pool, alloc[k]))
    return sorted(chosen)


def load_records(path):
    records = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{lineno}: invalid JSON: {exc}")
            for key in ("id", "business_id", "question"):
                if key not in rec:
                    raise SystemExit(f"{path}:{lineno}: missing key {key!r}")
            records.append(rec)
    return records


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--questions", required=True, help="input questions.jsonl")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--sample", type=int, default=500, help="number of originals to sample")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)

    records = load_records(args.questions)
    selected = sample_indices(records, args.sample, args.seed)

    out_lines = []
    mapping = {}
    kind_counts = Counter()
    skip_counts = Counter()
    for idx in selected:
        rec = records[idx]
        out_lines.append(rec)
        for kind, (text, reason) in make_variants(rec["question"]).items():
            if text is None:
                skip_counts[f"{kind}:{reason}"] += 1
                continue
            vid = f"{rec['id']}__{kind}"
            variant = dict(rec)
            variant["id"] = vid
            variant["question"] = text
            out_lines.append(variant)
            mapping[vid] = rec["id"]
            kind_counts[kind] += 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "questions.jsonl", "w", encoding="utf-8") as fh:
        for rec in out_lines:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    with open(out_dir / "variants.json", "w", encoding="utf-8") as fh:
        json.dump(mapping, fh, ensure_ascii=False, indent=2)

    summary = {
        "originals": len(selected),
        "variants": dict(kind_counts),
        "skipped": dict(sorted(skip_counts.items())),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
