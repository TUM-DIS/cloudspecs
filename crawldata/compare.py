#!/usr/bin/env python3
"""
Compare two cloudspecs.duckdb files (for debugging the rewrite).

A correct rewrite should differ from the old database only by *newer* instances
(rows present in NEW but not OLD). This tool reports:
  - instances only in OLD (dropped)
  - instances only in NEW (added / newer)
  - per-column value differences for instances present in both

Usage: python compare.py OLD.duckdb NEW.duckdb
"""

import sys
import duckdb

KEY = "instance"
# small absolute tolerance for floating-point columns
FLOAT_TOL = 1e-6


def load(path):
    con = duckdb.connect(path, read_only=True)
    cols = [r[0] for r in con.execute("describe aws_all").fetchall()]
    key_idx = cols.index(KEY)
    rows = {r[key_idx]: r for r in con.execute("select * from aws_all").fetchall()}
    con.close()
    return cols, rows


def approx_equal(a, b):
    if a == b:
        return True
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= FLOAT_TOL
    return False


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 1
    old_path, new_path = sys.argv[1], sys.argv[2]
    old_cols, old = load(old_path)
    new_cols, new = load(new_path)

    print(f"OLD {old_path}: {len(old)} rows, {len(old_cols)} cols")
    print(f"NEW {new_path}: {len(new)} rows, {len(new_cols)} cols")

    if old_cols != new_cols:
        print("\n!! COLUMN MISMATCH")
        print("  only in OLD:", set(old_cols) - set(new_cols))
        print("  only in NEW:", set(new_cols) - set(old_cols))

    only_old = sorted(set(old) - set(new))
    only_new = sorted(set(new) - set(old))
    print(f"\nonly in OLD (dropped): {len(only_old)}")
    if only_old:
        print("  " + ", ".join(only_old))
    print(f"only in NEW (added / newer): {len(only_new)}")
    if only_new:
        print("  " + ", ".join(only_new[:60]) + (" ..." if len(only_new) > 60 else ""))

    # per-column diffs on shared instances
    shared = sorted(set(old) & set(new))
    common_cols = [c for c in old_cols if c in new_cols]
    per_col = {c: 0 for c in common_cols}
    examples = {c: [] for c in common_cols}
    for inst in shared:
        orow = dict(zip(old_cols, old[inst]))
        nrow = dict(zip(new_cols, new[inst]))
        for c in common_cols:
            if c == KEY:
                continue
            if not approx_equal(orow[c], nrow[c]):
                per_col[c] += 1
                if len(examples[c]) < 5:
                    examples[c].append((inst, orow[c], nrow[c]))

    print(f"\nvalue differences among {len(shared)} shared instances:")
    any_diff = False
    for c in common_cols:
        if per_col[c]:
            any_diff = True
            print(f"  {c}: {per_col[c]} differ")
            for inst, o, n in examples[c]:
                print(f"      {inst}: old={o!r} new={n!r}")
    if not any_diff:
        print("  none - shared instances are identical")
    return 0


if __name__ == "__main__":
    sys.exit(main())
