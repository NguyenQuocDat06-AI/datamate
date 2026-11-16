"""Audit Competition Rule 6 compliance in bizreason."""

import os
import re
import sys


def main() -> int:
    patterns = {
        "question_id": re.compile(r"q_[0-9a-f]{8,}", re.IGNORECASE),
        "business_id": re.compile(r"\b(biz_\d+|dev_\d+)\b", re.IGNORECASE),
        "dataset_names": re.compile(
            r"\b(public_test|live_trammay_eval)\b", re.IGNORECASE
        ),
    }

    matches = []
    base_dir = os.path.join(os.path.dirname(__file__), "..", "bizreason")
    target_paths = [base_dir]
    replay_path = os.path.join(os.path.dirname(__file__), "replay_1000.py")
    if os.path.exists(replay_path):
        target_paths.append(replay_path)

    for target in target_paths:
        if os.path.isfile(target):
            files_to_check = [(os.path.dirname(target), os.path.basename(target))]
        else:
            files_to_check = []
            for root, _, files in os.walk(target):
                for f in files:
                    if f.endswith(".py"):
                        files_to_check.append((root, f))

        for root, f in files_to_check:
            p = os.path.join(root, f)
            with open(p, "r", encoding="utf-8") as fp:
                for line_no, line in enumerate(fp, 1):
                    for name, pat in patterns.items():
                        m = pat.search(line)
                        if m:
                            matches.append(
                                (name, p, line_no, m.group(0), line.strip())
                            )

    if matches:
        print(f"FAIL: Found {len(matches)} hardcoded matches in bizreason/:")
        for name, p, line_no, match_str, line in matches:
            print(f"[{name}] {p}:{line_no} -> {match_str} in '{line}'")
        return 1

    print("PASS: 0 hardcoding matches found in bizreason/!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
