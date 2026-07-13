#!/usr/bin/env python3
"""
Get Hetzner Cloud server (VPS) instance data.

Standalone Hetzner counterpart to build.py / gcp.py / azure.py / stackit.py / ovh.py /
oracle.py. Writes a `hetzner_all` table + views into DuckDB with columns matching aws_all
exactly, so all seven clouds are directly comparable.

Source: the Hetzner Cloud API `GET /v1/server_types`, which returns every cloud server
type with its full spec (vCPU `cores`, `memory` GB, local-NVMe `disk` GB, `cpu_type`
shared/dedicated, `architecture` x86/arm, deprecation) and per-location hourly prices.
Unlike STACKIT/OVHcloud this API needs a token (any read-only Hetzner Cloud API token):
set HCLOUD_TOKEN or put a token in ../hetzner.txt, then run with --refresh. Without a token
the build uses the SNAPSHOT below -- the fsn1 server_types captured live from this API on
2026-07-13 -- so the DB is always produced; --refresh re-fetches live and overwrites it.

Prices are the compute-only server price (Hetzner bills the primary IPv4 separately) for a
low-price EU reference location (Falkenstein fsn1); the API also carries nbg1/hel1 (same
EU price) and the pricier ash/hil/sin. Prices are published in EUR and converted to USD
(to match the other clouds) at the live ECB reference rate, overridable with --eur-usd.

Derived / static: family (name minus the size digits: cx22 -> cx, ccx13 -> ccx), category
(shared vs dedicated vCPU), processor_model and release_year (curated per family -- the API
gives neither).

Output objects (mirroring the other builds):
  hetzner_all    every server type, all 27 aws_all columns.
  hetzner        comparable slice: current, priced, dedicated-vCPU (CCX) -- the shared-vCPU
                 lines are oversubscribed and excluded, like the other clouds' burst tiers.
  hetzner_family one representative type per family (largest; net-efficiency window like
                 aws_family, degrades to largest since net_gbitps is null).
  hetzner_accel  GPU types -- empty (Hetzner Cloud has no GPU servers).
  hetzner_burst  the shared-vCPU lines (CX / CPX / CAX) -- Hetzner's oversubscribed tier.

Notes vs AWS: net_gbitps / net_peak_gbitps and ebs_* are always null -- Hetzner publishes
no per-type network bandwidth, and block-storage (Volumes) throughput is per-volume, not
per-type. cores (physical) is inferred from the CPU model's SMT (Ampere Altra = 1
thread/core, Intel/AMD x86 = 2), since Hetzner publishes only vCPUs. storage_gb is the
included local NVMe (always SSD/NVMe). accelerators is
always 0. vcpus_base == vcpus -- shared vCPUs run at full speed (no throttled baseline);
they are flagged via hetzner_burst, not a fractional baseline.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))

API_URL = "https://api.hetzner.cloud/v1/server_types"
DEFAULT_LOCATION = "fsn1"          # Falkenstein; low-price EU reference (== nbg1 / hel1)
DEFAULT_TOKEN_FILE = os.path.join(HERE, "..", "hetzner.txt")

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
FALLBACK_EUR_USD = 1.14            # ~mid-2026; used only if ECB is unreachable

WORK = os.path.join(HERE, "work", "hetzner")

# Shared-vCPU lines are oversubscribed (Hetzner's cheap tier) -> the "burst" analog,
# excluded from the comparable `hetzner` view. Dedicated (CCX) stays in.
SHARED_FAMILIES = {"cx", "cpx", "cax"}

# Curated per-family CPU model + launch year (the API exposes cpu_type/architecture but no
# vendor or date). CX gen3 deliberately runs on mixed recycled Intel Xeon Gold / AMD
# silicon; CAX is Ampere Altra; CCX is AMD EPYC. CPX is the exception: gen1 (name ending in
# "1", AMD Rome) and gen2 (ending in "2", AMD Genoa) are both currently sold, so its CPU and
# year are resolved per generation by _proc / _year below.
_CPU_BY_FAMILY = {
    "cx": "Intel Xeon Gold / AMD EPYC",
    "cax": "Ampere Altra",
    "ccx": "AMD EPYC (Milan/Genoa)",
}
_YEAR_BY_FAMILY = {
    "cx": 2025.0,   # CX gen3 (cx23-53), Oct 2025
    "cax": 2023.0,  # CAX (Ampere Arm), Apr 2023
    "ccx": 2025.0,  # CCX gen3 (ccx13-63)
}


def _proc(name, fam):
    if fam == "cpx":                       # gen1 (…1) = Rome, gen2 (…2) = Genoa
        return "AMD EPYC (Rome)" if name.endswith("1") else "AMD EPYC (Genoa)"
    return _CPU_BY_FAMILY.get(fam)


def _year(name, fam):
    if fam == "cpx":
        return 2020.0 if name.endswith("1") else 2025.0
    return _YEAR_BY_FAMILY.get(fam)


def _cores(vcpus, proc, arch):
    """Physical cores inferred from the CPU's threads-per-core: Ampere Altra (CAX) is
    single-threaded per core (cores = vcpus); Intel Xeon and AMD EPYC (CX / CPX / CCX) run
    2-way SMT, so a vCPU is one hyperthread and cores = vcpus / 2."""
    no_smt = (proc and ("Ampere" in proc or "Altra" in proc)) or arch == "arm64"
    return vcpus if no_smt else (vcpus + 1) // 2   # ceil: 3 threads span 2 cores


# Snapshot of GET /v1/server_types at the EU reference location (fsn1), captured live on
# 2026-07-13 (net EUR/hour, excl. primary IPv4). Used when no HCLOUD_TOKEN is available so
# the DB is always produced; --refresh with a token re-fetches live (and would additionally
# pick up any deprecated types, flagged is_current = false -- currently the API lists none).
# Fields: (name, cpu_type, arch, cores, memory_gb, disk_gb, price_eur_hourly, deprecated)
SNAPSHOT = [
    # CX -- shared vCPU, x86 (gen3, mixed recycled Intel/AMD)
    ("cx23", "shared", "x86_64", 2, 4.0, 40, 0.0088, False),
    ("cx33", "shared", "x86_64", 4, 8.0, 80, 0.0136, False),
    ("cx43", "shared", "x86_64", 8, 16.0, 160, 0.0256, False),
    ("cx53", "shared", "x86_64", 16, 32.0, 320, 0.0473, False),
    # CAX -- shared vCPU, Arm (Ampere Altra)
    ("cax11", "shared", "arm64", 2, 4.0, 40, 0.0096, False),
    ("cax21", "shared", "arm64", 4, 8.0, 80, 0.0168, False),
    ("cax31", "shared", "arm64", 8, 16.0, 160, 0.0336, False),
    ("cax41", "shared", "arm64", 16, 32.0, 320, 0.0657, False),
    # CPX gen1 -- shared vCPU, x86 (AMD Rome)
    ("cpx11", "shared", "x86_64", 2, 2.0, 40, 0.0088, False),
    ("cpx21", "shared", "x86_64", 3, 4.0, 80, 0.0152, False),
    ("cpx31", "shared", "x86_64", 4, 8.0, 160, 0.0280, False),
    ("cpx41", "shared", "x86_64", 8, 16.0, 240, 0.0521, False),
    ("cpx51", "shared", "x86_64", 16, 32.0, 360, 0.1138, False),
    # CPX gen2 -- shared vCPU, x86 (AMD Genoa)
    ("cpx12", "shared", "x86_64", 1, 2.0, 40, 0.0184, False),
    ("cpx22", "shared", "x86_64", 2, 4.0, 80, 0.0312, False),
    ("cpx32", "shared", "x86_64", 4, 8.0, 160, 0.0569, False),
    ("cpx42", "shared", "x86_64", 8, 16.0, 320, 0.1114, False),
    ("cpx52", "shared", "x86_64", 12, 24.0, 480, 0.1610, False),
    ("cpx62", "shared", "x86_64", 16, 32.0, 640, 0.2083, False),
    # CCX -- dedicated vCPU, x86 (gen3, AMD EPYC)
    ("ccx13", "dedicated", "x86_64", 2, 8.0, 80, 0.0689, False),
    ("ccx23", "dedicated", "x86_64", 4, 16.0, 160, 0.1378, False),
    ("ccx33", "dedicated", "x86_64", 8, 32.0, 240, 0.2219, False),
    ("ccx43", "dedicated", "x86_64", 16, 64.0, 360, 0.4423, False),
    ("ccx53", "dedicated", "x86_64", 32, 128.0, 600, 0.8550, False),
    ("ccx63", "dedicated", "x86_64", 48, 192.0, 960, 1.3678, False),
]


# --------------------------------------------------------------------------- #
# HTTP + caching
# --------------------------------------------------------------------------- #
def _get(url, timeout=60, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# EUR -> USD
# --------------------------------------------------------------------------- #
def eur_to_usd(explicit):
    """(rate, source). Explicit --eur-usd wins; else the live ECB rate; else fallback."""
    if explicit:
        return explicit, "--eur-usd"
    try:
        xml = _get(ECB_URL, timeout=20)
        m = re.search(r"currency=['\"]USD['\"]\s+rate=['\"]([0-9.]+)['\"]", xml)
        d = re.search(r"time=['\"]([0-9-]+)['\"]", xml)
        if m:
            return float(m.group(1)), f"ECB {d.group(1) if d else 'daily'}"
    except Exception as e:
        print(f"  WARNING: ECB rate unavailable ({e}); using fallback {FALLBACK_EUR_USD}")
    return FALLBACK_EUR_USD, "fallback"


# --------------------------------------------------------------------------- #
# Server types (live API or frozen snapshot)
# --------------------------------------------------------------------------- #
def _find_token(explicit):
    if explicit:
        return explicit
    tok = os.environ.get("HCLOUD_TOKEN")
    if tok:
        return tok.strip()
    if os.path.exists(DEFAULT_TOKEN_FILE):
        with open(DEFAULT_TOKEN_FILE) as f:
            return f.read().strip()
    return None


def _fetch_live(token, location):
    """Page through GET /v1/server_types and normalize to snapshot tuples for `location`."""
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "cloudspecs"}
    out, page = [], 1
    while True:
        data = json.loads(_get(f"{API_URL}?page={page}&per_page=50", headers=headers))
        for st in data.get("server_types", []):
            price = next((p for p in st.get("prices", [])
                          if p.get("location") == location), None)
            if not price:                                  # not offered in this location
                continue
            eur = float(price["price_hourly"]["net"])
            dep = bool(st.get("deprecated")) or bool(st.get("deprecation"))
            out.append((st["name"], st["cpu_type"],
                        "arm64" if st["architecture"] == "arm" else "x86_64",
                        int(st["cores"]), float(st["memory"]), int(st["disk"]), eur, dep))
        pg = (data.get("meta") or {}).get("pagination") or {}
        if not pg.get("next_page"):
            break
        page = pg["next_page"]
    return out


def load_server_types(token, location, refresh):
    """Return (list of snapshot tuples, source-label). Live API when a token is present
    (cached under work/hetzner); otherwise the frozen SNAPSHOT."""
    cache = os.path.join(WORK, f"server_types-{location}.json")
    if token and (refresh or not os.path.exists(cache)):
        print(f"Fetching Hetzner Cloud server_types (location={location}) ...")
        types = _fetch_live(token, location)
        os.makedirs(WORK, exist_ok=True)
        with open(cache, "w") as f:
            json.dump(types, f)
        time.sleep(0.15)
        return types, "live API"
    if os.path.exists(cache):
        with open(cache) as f:
            return [tuple(t) for t in json.load(f)], "cache"
    if not token:
        print("  no HCLOUD_TOKEN / ../hetzner.txt -- using the frozen SNAPSHOT "
              "(set a token and --refresh for live data)")
    return list(SNAPSHOT), "snapshot"


# --------------------------------------------------------------------------- #
# Assemble
# --------------------------------------------------------------------------- #
# Columns match aws_all exactly (same names, same order).
COLUMNS = [
    ("instance", "VARCHAR"), ("family", "VARCHAR"), ("category", "VARCHAR"),
    ("price_hour", "DOUBLE"), ("ram_gib", "DOUBLE"), ("vcpus", "BIGINT"),
    ("vcpus_base", "DOUBLE"), ("cores", "BIGINT"), ("processor_model", "VARCHAR"),
    ("arch", "VARCHAR"), ("net_gbitps", "DOUBLE"), ("net_peak_gbitps", "DOUBLE"),
    ("storage_gb", "BIGINT"), ("storage_count", "BIGINT"),
    ("storage_is_ssd", "BOOLEAN"), ("storage_is_nvme", "BOOLEAN"),
    ("ebs_iops", "BIGINT"), ("ebs_gbitps", "DOUBLE"),
    ("ebs_peak_iops", "BIGINT"), ("ebs_peak_gbitps", "DOUBLE"),
    ("accelerators", "BIGINT"), ("accelerator_model", "VARCHAR"),
    ("accelerator_gib", "DOUBLE"), ("is_current", "BOOLEAN"),
    ("storage_read_iops", "BIGINT"), ("storage_write_iops", "BIGINT"),
    ("release_year", "DOUBLE"),
]


def _family(name):
    """Family = server-type name minus its size digits (cx22 -> cx, ccx13 -> ccx)."""
    return re.sub(r"\d+$", "", name)


def build_rows(server_types, rate):
    """One 27-column row per server type. Returns (rows, shared_families)."""
    rows = []
    shared = set()
    for (name, cpu_type, arch, cores, memory, disk, eur, deprecated) in sorted(server_types):
        fam = _family(name)
        if cpu_type == "shared":
            shared.add(fam)
        proc = _proc(name, fam)
        rows.append((
            name,                                          # instance
            fam,                                           # family
            "Shared vCPU" if cpu_type == "shared" else "Dedicated vCPU",  # category
            round(eur * rate, 6),                          # price_hour (EUR -> USD)
            float(memory),                                 # ram_gib
            cores,                                          # vcpus (API "cores" = vCPUs)
            float(cores),                                  # vcpus_base (full-speed vCPUs)
            _cores(cores, proc, arch),                      # cores (physical, inferred)
            proc,                                           # processor_model
            arch,                                           # arch
            None, None,                                     # net_gbitps / net_peak_gbitps
            disk or None,                                   # storage_gb (local NVMe)
            1 if disk else None,                            # storage_count
            True if disk else None,                         # storage_is_ssd
            True if disk else None,                         # storage_is_nvme
            None, None, None, None,                         # ebs_iops/gbitps/peak_iops/peak_gbitps
            0,                                              # accelerators
            None,                                           # accelerator_model
            None,                                           # accelerator_gib
            not deprecated,                                 # is_current
            None, None,                                     # storage_read_iops / write_iops
            _year(name, fam),                               # release_year
        ))
    print(f"  {len(rows)} server types; shared-vCPU families: {', '.join(sorted(shared))}")
    return rows, sorted(shared)


def views_sql(shared_families):
    burst = ", ".join(f"'{f}'" for f in shared_families) or "''"
    return f"""
create view hetzner as
  select instance, family, category, price_hour, release_year, ram_gib, vcpus, cores, processor_model, arch, net_gbitps, net_peak_gbitps, storage_gb, storage_count, storage_read_iops, storage_write_iops, storage_is_ssd, storage_is_nvme, ebs_iops, ebs_gbitps, ebs_peak_iops, ebs_peak_gbitps
  from hetzner_all
  where is_current
  and price_hour is not null
  and accelerators = 0
  and family not in ({burst});
create view hetzner_family as
  select * from hetzner join
  (select case when two.instance is null then one.instance
              when ((two.net_gbitps/two.price_hour)/(one.net_gbitps/one.price_hour) > 1.1) then two.instance
              else one.instance end as instance
  from (select * from (select *, row_number() over (partition by family order by vcpus desc) r from hetzner) where r = 1) one
  left join (select * from (select *, row_number() over (partition by family order by vcpus desc) r from hetzner) where r = 2) two
  using (family)) using (instance);
create view hetzner_accel as
  select * from hetzner_all
  where category = 'GPU' and accelerator_model is not null;
create view hetzner_burst as
  select * from hetzner_all where family in ({burst});
COMMENT ON COLUMN hetzner_all.instance IS 'Hetzner Cloud server type name (e.g. cx22, ccx33)';
COMMENT ON COLUMN hetzner_all.price_hour IS 'Reference-location (fsn1) compute hourly price in USD (server price without the separate primary IPv4, converted from EUR at the ECB reference rate)';
COMMENT ON COLUMN hetzner_all.family IS 'Server-type family: name minus the size digits (cx22 -> cx, ccx13 -> ccx)';
COMMENT ON COLUMN hetzner_all.category IS 'Shared vCPU (CX/CPX/CAX, oversubscribed) or Dedicated vCPU (CCX)';
COMMENT ON COLUMN hetzner_all.ram_gib IS 'Amount of main memory in GiB';
COMMENT ON COLUMN hetzner_all.vcpus IS 'Number of vCPUs';
COMMENT ON COLUMN hetzner_all.vcpus_base IS 'Same as vcpus (shared vCPUs run at full speed; the shared lines are flagged via hetzner_burst)';
COMMENT ON COLUMN hetzner_all.cores IS 'Physical cores inferred from the CPU: Ampere Altra (CAX) has no SMT so cores = vcpus; Intel Xeon / AMD EPYC (CX/CPX/CCX) are 2-way SMT so cores = vcpus/2';
COMMENT ON COLUMN hetzner_all.processor_model IS 'CPU model of the family (curated; the API gives only cpu_type and architecture)';
COMMENT ON COLUMN hetzner_all.arch IS 'Processor architecture (x86_64 or arm64)';
COMMENT ON COLUMN hetzner_all.net_gbitps IS 'Always NULL -- Hetzner publishes no per-type network bandwidth';
COMMENT ON COLUMN hetzner_all.net_peak_gbitps IS 'Always NULL (see net_gbitps)';
COMMENT ON COLUMN hetzner_all.storage_gb IS 'Included local NVMe SSD in GB';
COMMENT ON COLUMN hetzner_all.storage_count IS 'Number of local disks (1)';
COMMENT ON COLUMN hetzner_all.storage_is_ssd IS 'Whether local storage is SSD (always true)';
COMMENT ON COLUMN hetzner_all.storage_is_nvme IS 'Whether local storage is directly-attached NVMe (always true)';
COMMENT ON COLUMN hetzner_all.ebs_iops IS 'Always NULL -- Hetzner Volume throughput is per-volume, not per-type';
COMMENT ON COLUMN hetzner_all.ebs_gbitps IS 'Always NULL (see ebs_iops)';
COMMENT ON COLUMN hetzner_all.ebs_peak_iops IS 'Always NULL (see ebs_iops)';
COMMENT ON COLUMN hetzner_all.ebs_peak_gbitps IS 'Always NULL (see ebs_iops)';
COMMENT ON COLUMN hetzner_all.accelerators IS 'Always 0 -- Hetzner Cloud has no GPU servers';
COMMENT ON COLUMN hetzner_all.accelerator_model IS 'Always NULL (no GPU servers)';
COMMENT ON COLUMN hetzner_all.accelerator_gib IS 'Always NULL (no GPU servers)';
COMMENT ON COLUMN hetzner_all.is_current IS 'Whether the type is current (not deprecated / still orderable)';
COMMENT ON COLUMN hetzner_all.storage_read_iops IS 'Always NULL -- not published';
COMMENT ON COLUMN hetzner_all.storage_write_iops IS 'Always NULL -- not published';
COMMENT ON COLUMN hetzner_all.release_year IS 'Approximate generation launch year (curated; the API publishes no launch date)';
"""


def write_duckdb(rows, shared_families, out_path):
    con = duckdb.connect(out_path)
    cols_ddl = ", ".join(f'"{n}" {t}' for n, t in COLUMNS)
    for v in ("hetzner_family", "hetzner_accel", "hetzner_burst", "hetzner"):
        con.execute(f"drop view if exists {v}")
    con.execute("drop table if exists hetzner_all cascade")
    con.execute(f"create table hetzner_all ({cols_ddl})")
    con.executemany(
        f"insert into hetzner_all values ({', '.join(['?'] * len(COLUMNS))})", rows
    )
    con.execute(views_sql(shared_families))
    counts = {
        v: con.execute(f"select count(*) from {v}").fetchone()[0]
        for v in ("hetzner_all", "hetzner", "hetzner_family", "hetzner_accel", "hetzner_burst")
    }
    con.close()
    print("Wrote " + out_path)
    for v, c in counts.items():
        print(f"  {v}: {c}")


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--output", default=os.path.join(HERE, "cloudspecs.duckdb"),
                    help="DuckDB file to write the hetzner_all table + views into")
    ap.add_argument("--location", default=DEFAULT_LOCATION,
                    help="Hetzner location to price (default fsn1; EU is cheapest)")
    ap.add_argument("--token", default=None,
                    help="Hetzner Cloud API token (default: HCLOUD_TOKEN or ../hetzner.txt)")
    ap.add_argument("--eur-usd", type=float, default=None,
                    help="EUR->USD rate (default: live ECB reference rate)")
    ap.add_argument("--refresh", action="store_true",
                    help="re-fetch server_types live (requires a token)")
    args = ap.parse_args()

    rate, src = eur_to_usd(args.eur_usd)
    print(f"EUR->USD = {rate} ({src})")

    token = _find_token(args.token)
    types, source = load_server_types(token, args.location, args.refresh)
    print(f"  {len(types)} server types ({source})")

    print("Building rows ...")
    rows, shared = build_rows(types, rate)
    write_duckdb(rows, shared, args.output)


if __name__ == "__main__":
    sys.exit(main())
