#!/usr/bin/env python3
"""
Combine the per-cloud tables/views in cloudspecs.duckdb into cross-cloud views.

Run LAST, after build.py (AWS) and the other cloud scripts (gcp, azure, stackit,
ovh, oracle, hetzner) have all written their <cloud>_all table and
<cloud> / <cloud>_accel / <cloud>_shared views into the same cloudspecs.duckdb.

Creates four UNION ALL views spanning every cloud, each with a leading `cloud`
column tagging the source cloud:
  cloudspecs         the comparable slice     (union of the <cloud> views)
  cloudspecs_family  one type per family      (union of the <cloud>_family views)
  cloudspecs_accel   accelerator instances    (union of the <cloud>_accel views)
  cloudspecs_shared  shared / burstable-CPU   (union of the <cloud>_shared views)
  cloudspecs_all     every instance           (union of the <cloud>_all tables)

Because every cloud shares an identical column schema, `select '<cloud>' as
cloud, * from <object>` lines up column-for-column across the UNION ALL.

Output: cloudspecs.duckdb (adds the four cloudspecs* views).
"""

import argparse
import os
import sys

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))

# Source-cloud tags, emitted as a static string in the `cloud` column of each
# UNION ALL branch. Order also fixes the row order of the combined views.
CLOUDS = ["aws", "gcp", "azure", "stackit", "ovh", "oracle", "hetzner"]

# (combined view name, per-cloud object suffix). The comparable slice unions the
# bare <cloud> views; the others append the matching suffix.
COMBINED = [
    ("cloudspecs", ""),
    ("cloudspecs_family", "_family"),
    ("cloudspecs_accel", "_accel"),
    ("cloudspecs_shared", "_shared"),
    ("cloudspecs_all", "_all"),
]


def view_sql(view, suffix):
    branches = "\n  union all\n".join(
        f"  select '{c}' as cloud, * from {c}{suffix}" for c in CLOUDS
    )
    return f"create view {view} as\n{branches};"


def write_duckdb(out_path):
    con = duckdb.connect(out_path)
    for view, _ in COMBINED:
        con.execute(f"drop view if exists {view}")
    for view, suffix in COMBINED:
        con.execute(view_sql(view, suffix))
    counts = {
        view: con.execute(f"select count(*) from {view}").fetchone()[0]
        for view, _ in COMBINED
    }
    con.close()
    print("Wrote " + out_path)
    for v, c in counts.items():
        print(f"  {v}: {c}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--output", default=os.path.join(HERE, "cloudspecs.duckdb"))
    args = ap.parse_args()
    write_duckdb(args.output)


if __name__ == "__main__":
    sys.exit(main())
