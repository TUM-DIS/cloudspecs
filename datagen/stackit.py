#!/usr/bin/env python3
"""
Get STACKIT (Schwarz Group) Compute Engine instance data.

Standalone STACKIT counterpart to build.py / gcp.py / azure.py. Collects flavor
specs and the on-demand price in one reference region (eu01, Germany), and writes a
`stackit_all` table + views into DuckDB. Columns match aws_all exactly, so all four
clouds (AWS, GCP, Azure, STACKIT) are directly comparable.

Both data sources are PUBLIC (no auth) -- STACKIT's simplest trait vs the other clouds:

  1. Retail price list  (stackit.com price-list JSON) -> the flavor universe + core
       specs. Each Compute-Engine flavor entry carries flavor name, on-demand EUR/hour
       price, vCPU, RAM, hardware vendor (Intel/AMD/ARM/GPU) and the cpuOverprovisioning
       flag. We keep the standard (non-"metro") eu01 flavors, dropping the parallel
       "metro" (distributed-placement) meters that duplicate a flavor at a higher price.
  2. Machine-types docs (docs.stackit.cloud/.../machine-types) -> enrichment only:
       local disk size, GPU model + memory, and the per-generation CPU microarchitecture
       (e.g. "Intel Ice Lake", "AMD EPYC Genoa", "ARM Ampere Altra").

Prices are published in EUR; price_hour is converted to USD (to match the other three
clouds) using the live ECB reference rate (public XML), overridable with --eur-usd.

Derived / static: family (flavor name minus the size suffix), category (from the
variant letter / CPU:RAM ratio), physical cores (arm = 1 thread/core, x86 = 2),
release_year (CPU-generation launch year, approximate), accelerator count (from the
".gN" flavor suffix).

Output objects (mirroring the AWS/GCP/Azure builds):
  stackit_all    every flavor, all 27 aws_all columns.
  stackit        comparable slice: current, priced, non-GPU, non-burstable
                 (CPU-overprovisioned families).
  stackit_family one representative flavor per family (largest, net-efficiency
                 window like aws_family -- degrades to largest since net_gbitps is null).
  stackit_accel  GPU (n-series) flavors, with the GPU model.
  stackit_burst  CPU-overprovisioned ("burstable") families.

Notes vs AWS: net_gbitps / net_peak_gbitps and ebs_* are always null -- STACKIT
publishes no per-flavor network- or block-storage-throughput figures. processor_model
is the CPU microarchitecture (coarse vendor fallback where the docs omit a family).
release_year is the CPU generation's launch year (a proxy; STACKIT publishes no GA
date). vcpus_base == vcpus (no fractional-baseline burst model; overprovisioned
families are flagged via stackit_burst instead).
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import duckdb
from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_REGION = "eu01"  # STACKIT's primary German region; change with --region

# Public price-list ("SKU") JSON that powers calculator.stackit.cloud (no auth); the
# ?region= param filters server-side. Cached under work/. Same schema as the snapshot.
PRICE_URL = "https://pim.api.stackit.cloud/v1/skus"
PRICE_SNAPSHOT = os.path.join(HERE, "stackit-prices.json")  # offline fallback (optional)

# Machine-types docs -- the only source for local disk, GPU model and CPU microarch.
DOCS_URL = ("https://docs.stackit.cloud/products/compute-engine/server/"
            "basics/machine-types/")

# Live EUR->USD reference rate (public ECB XML), so price_hour matches the USD clouds.
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
FALLBACK_EUR_USD = 1.14  # ~mid-2026; used only if ECB is unreachable (or via --eur-usd)

WORK = os.path.join(HERE, "work", "stackit")

# Only these price-list categories are compute flavors (Database reuses the flavor
# attribute for DBaaS sizes and must be excluded).
COMPUTE_CATEGORIES = {"Compute Engine", "Compute Engine GPU", "Confidential Computing"}

# Category from the variant letter (first letter of the family); n-series is forced to
# GPU by the hardware field. Falls back to the CPU:RAM ratio for any unknown letter.
CATEGORY_BY_LETTER = {
    "t": "General purpose", "g": "General purpose",
    "s": "Compute optimized", "c": "Compute optimized",
    "m": "Memory optimized", "b": "Memory optimized", "u": "Memory optimized",
    "n": "GPU",
}

# CPU-generation launch year, used as an approximate release_year (STACKIT exposes no
# per-flavor GA date). Keyed by a substring of the docs CPU-microarchitecture string.
_CPU_YEAR = {
    "Broadwell": 2016.0,        # STACKIT gen1 Intel (deprecated)
    "Cascade Lake": 2019.0,
    "Ice Lake": 2021.0,         # gen2i
    "Sapphire Rapids": 2023.0,  # GPU hosts (n2/n3)
    "Emerald Rapids": 2024.0,   # gen3i
    "EPYC Rome": 2019.0,        # c1a
    "EPYC Milan": 2021.0,       # m1a
    "EPYC Genoa": 2023.0,       # m2a / b2a
    "EPYC Bergamo": 2023.0,     # c2a / g2a
    "Ampere Altra": 2021.0,     # g1r (ARM)
}

# Coarse processor_model when the docs list no CPU for a family (by price-list vendor).
_COARSE_PROC = {
    "Intel": "Intel Xeon", "AMD": "AMD EPYC", "ARM": "Ampere Altra", "GPU": "Intel Xeon",
}


# --------------------------------------------------------------------------- #
# HTTP + caching
# --------------------------------------------------------------------------- #
def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _cached(name, url, refresh):
    """Fetch `url` into work/stackit/<name>, reusing the cache unless refresh."""
    path = os.path.join(WORK, name)
    if not refresh and os.path.exists(path):
        with open(path) as f:
            return f.read()
    os.makedirs(WORK, exist_ok=True)
    text = _get(url)
    with open(path, "w") as f:
        f.write(text)
    time.sleep(0.15)
    return text


# --------------------------------------------------------------------------- #
# EUR -> USD
# --------------------------------------------------------------------------- #
def eur_to_usd(explicit):
    """Return (rate, source). Explicit --eur-usd wins; else the live ECB reference
    rate; else the hard-coded fallback (never fails the build)."""
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
# Prices (public price-list JSON)
# --------------------------------------------------------------------------- #
def fetch_prices(region, refresh):
    """Return the list of standard (non-metro) compute flavor price entries for
    `region`. Downloads the live price list (cached under work/stackit); falls back
    to a bundled snapshot if the endpoint is unreachable."""
    print(f"Fetching STACKIT price list ({region}) ...")
    url = f"{PRICE_URL}?region={urllib.parse.quote(region)}"
    try:
        raw = _cached(f"prices-{region}.json", url, refresh)
    except Exception as e:
        if not os.path.exists(PRICE_SNAPSHOT):
            raise SystemExit(f"Price list unavailable ({e}) and no snapshot at "
                             f"{PRICE_SNAPSHOT}")
        print(f"  WARNING: live price list unreachable ({e}); using snapshot")
        with open(PRICE_SNAPSHOT) as f:
            raw = f.read()
    data = json.loads(raw)
    print(f"  price list dated {data.get('lastUpdatedAt', '?')}")
    flavors = []
    for s in data.get("services", []):
        a = s.get("attributes") or {}
        if not a.get("flavor") or s.get("region") != region:
            continue
        if s.get("category") not in COMPUTE_CATEGORIES:
            continue
        if a.get("metro"):                     # drop the parallel metro meters
            continue
        flavors.append(s)
    print(f"  {len(flavors)} standard compute flavors in {region}")
    return flavors


# --------------------------------------------------------------------------- #
# Specs enrichment (machine-types docs)
# --------------------------------------------------------------------------- #
def _table_rows(table):
    """Yield (header_list, {header: value}) for a docs table, reconstructing the
    grouped rows (continuation rows omit the leading Variant / Type-description
    cells, which STACKIT renders as a vertical rowspan)."""
    trs = table.find_all("tr")
    if not trs:
        return
    hdr = [c.get_text(" ", strip=True) for c in trs[0].find_all(["th", "td"])]
    n = len(hdr)
    last = {}
    for tr in trs[1:]:
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        if not cells:
            continue
        missing = n - len(cells)
        if missing in (1, 2):
            cells = [last.get(hdr[i], "") for i in range(missing)] + cells
        if len(cells) != n:
            continue
        row = dict(zip(hdr, cells))
        for i in range(min(2, n)):             # remember group-header (Variant/desc)
            if row.get(hdr[i]):
                last[hdr[i]] = row[hdr[i]]
        yield hdr, row


def _fam(flavor):
    """Family = flavor name minus its size suffix (c2i.4 -> c2i, n1.14d.g1 -> n1)."""
    return flavor.split(".")[0]


def parse_docs(refresh):
    """Return (cpu_by_family, gpu_by_family, disk_by_flavor) from the machine-types
    page. cpu_by_family[fam] = (microarch, is_deprecated); gpu_by_family[fam] =
    (model, per_gpu_gib); disk_by_flavor[flavor] = local_disk_gb."""
    print("Parsing machine-types docs (CPU / GPU / local disk) ...")
    try:
        soup = BeautifulSoup(_cached("machine-types.html", DOCS_URL, refresh), "html.parser")
    except Exception as e:
        print(f"  WARNING: docs unavailable ({e}); processor/GPU/disk left coarse/null")
        return {}, {}, {}

    cpu, gpu, disk = {}, {}, {}
    gpu_re = re.compile(r"(\d+)\s*x\s*(.+?)\s+(\d+)\s*GB", re.IGNORECASE)
    for table in soup.find_all("table"):
        hdr = [c.get_text(" ", strip=True) for c in
               (table.find("tr").find_all(["th", "td"]) if table.find("tr") else [])]

        if hdr[:2] == ["Type version", "CPU architecture"]:
            for _, row in _table_rows(table):
                arch = row["CPU architecture"]
                dep = "deprecated" in arch.lower()
                model = re.sub(r"\s*\(deprecated\)\s*", "", arch, flags=re.I).strip()
                for tv in row["Type version"].split(","):
                    tv = tv.strip()
                    if tv:
                        cpu[_fam(tv)] = (model, dep)

        elif "GPU" in hdr and "Type names" in hdr:
            for _, row in _table_rows(table):
                m = gpu_re.search(row.get("GPU", ""))
                if not m:
                    continue
                model = re.sub(r"\s*Tensor Core-?GPU\s*", "", m.group(2), flags=re.I).strip()
                gpu[_fam(row["Type names"])] = (model, int(m.group(3)))

        if "Local disk (GB)" in hdr and "Type names" in hdr:
            for _, row in _table_rows(table):
                v = row.get("Local disk (GB)", "").replace(".", "").strip()
                if v.isdigit():
                    disk[row["Type names"].strip()] = int(v)

    print(f"  CPU for {len(cpu)} families, GPU for {len(gpu)} families, "
          f"local disk for {len(disk)} flavors")
    return cpu, gpu, disk


# --------------------------------------------------------------------------- #
# Assemble
# --------------------------------------------------------------------------- #
# Columns match aws_all exactly (same names, same order), so stackit_all is directly
# comparable to aws_all / gcp_all / azure_all.
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


def _category(fam, ratio):
    cat = CATEGORY_BY_LETTER.get(fam[0])
    if cat:
        return cat
    if ratio <= 2:
        return "Compute optimized"
    if ratio <= 4:
        return "General purpose"
    return "Memory optimized"


def _release_year(proc):
    for gen, year in _CPU_YEAR.items():
        if gen in proc:
            return year
    return None


def build_rows(flavors, rate, cpu_by_fam, gpu_by_fam, disk_by_flavor):
    rows = []
    burst_families = set()
    for s in sorted(flavors, key=lambda x: x["attributes"]["flavor"]):
        a = s["attributes"]
        flavor = a["flavor"]
        fam = _fam(flavor)
        hw = a.get("hardware")                         # Intel / AMD / ARM / GPU
        vcpus = int(a["vCPU"])
        ram = float(a["ram"])
        arch = "arm64" if hw == "ARM" else "x86_64"
        cores = vcpus if arch == "arm64" else max(1, vcpus // 2)

        docs_cpu = cpu_by_fam.get(fam)
        proc, cpu_dep = docs_cpu if docs_cpu else (_COARSE_PROC.get(hw, "Intel Xeon"), False)

        is_gpu = hw == "GPU"
        gm = re.search(r"\.g(\d+)$", flavor)
        n_gpu = int(gm.group(1)) if gm else (1 if is_gpu else 0)
        g_model, g_gib = gpu_by_fam.get(fam, (None, None)) if is_gpu else (None, None)
        acc_gib = (n_gpu * g_gib) if (is_gpu and g_gib) else None

        category = "GPU" if is_gpu else _category(fam, ram / vcpus if vcpus else 0)

        s_gb = disk_by_flavor.get(flavor)
        has_disk = s_gb is not None
        if a.get("cpuOverprovisioning"):
            burst_families.add(fam)

        rows.append((
            flavor,                                    # instance
            fam,                                       # family
            category,                                  # category
            round(float(s["price"]) * rate, 6),        # price_hour (EUR -> USD)
            ram,                                        # ram_gib
            vcpus,                                      # vcpus
            float(vcpus),                              # vcpus_base (no fractional model)
            cores,                                      # cores
            proc,                                       # processor_model
            arch,                                       # arch
            None, None,                                # net_gbitps / net_peak_gbitps
            s_gb,                                       # storage_gb
            1 if has_disk else None,                   # storage_count
            True if has_disk else None,                # storage_is_ssd
            True if has_disk else None,                # storage_is_nvme (local = NVMe)
            None, None, None, None,                    # ebs_iops/gbitps/peak_iops/peak_gbitps
            n_gpu if is_gpu else 0,                     # accelerators
            g_model,                                    # accelerator_model
            acc_gib,                                    # accelerator_gib
            not ((s.get("deprecated") == "Yes") or cpu_dep),  # is_current
            None, None,                                # storage_read_iops / write_iops
            None, # _release_year(proc),               # release_year
        ))
    priced = len(rows)
    n_gpu = sum(1 for r in rows if r[2] == "GPU")
    print(f"  {priced} flavors ({n_gpu} GPU); overprovisioned families: "
          f"{', '.join(sorted(burst_families))}")
    return rows, sorted(burst_families)


def views_sql(burst_families):
    burst = ", ".join(f"'{f}'" for f in burst_families) or "''"
    return f"""
create view stackit as
  select instance, family, category, price_hour, release_year, ram_gib, vcpus, cores, processor_model, arch, net_gbitps, net_peak_gbitps, storage_gb, storage_count, storage_read_iops, storage_write_iops, storage_is_ssd, storage_is_nvme, ebs_iops, ebs_gbitps, ebs_peak_iops, ebs_peak_gbitps
  from stackit_all
  where is_current
  and price_hour is not null
  and accelerators = 0
  and category != 'GPU'
  and family not in ({burst});
create view stackit_family as
  select * from stackit join
  (select case when two.instance is null then one.instance
              when ((two.net_gbitps/two.price_hour)/(one.net_gbitps/one.price_hour) > 1.1) then two.instance
              else one.instance end as instance
  from (select * from (select *, row_number() over (partition by family order by vcpus desc) r from stackit) where r = 1) one
  left join (select * from (select *, row_number() over (partition by family order by vcpus desc) r from stackit) where r = 2) two
  using (family)) using (instance);
create view stackit_accel as
  select * from stackit_all
  where category = 'GPU' and accelerator_model is not null;
create view stackit_burst as
  select * from stackit_all where family in ({burst});
COMMENT ON COLUMN stackit_all.instance IS 'STACKIT flavor name (e.g. c2i.4)';
COMMENT ON COLUMN stackit_all.price_hour IS 'eu01 on-demand price per hour in USD (converted from the EUR price list at the ECB reference rate)';
COMMENT ON COLUMN stackit_all.family IS 'Flavor family: flavor name minus the size suffix (c2i.4 -> c2i, n1.14d.g1 -> n1)';
COMMENT ON COLUMN stackit_all.category IS 'Category from the variant letter / CPU:RAM ratio (General purpose, Compute optimized, Memory optimized, GPU)';
COMMENT ON COLUMN stackit_all.ram_gib IS 'Amount of main memory in GiB';
COMMENT ON COLUMN stackit_all.vcpus IS 'Number of vCPUs';
COMMENT ON COLUMN stackit_all.vcpus_base IS 'Same as vcpus (STACKIT publishes no fractional baseline; overprovisioned families are in stackit_burst)';
COMMENT ON COLUMN stackit_all.cores IS 'Physical cores (ARM = 1 thread/core, x86 = 2 threads/core)';
COMMENT ON COLUMN stackit_all.processor_model IS 'CPU microarchitecture (from the docs; coarse vendor where a family is undocumented)';
COMMENT ON COLUMN stackit_all.arch IS 'Processor architecture';
COMMENT ON COLUMN stackit_all.net_gbitps IS 'Always NULL -- STACKIT publishes no per-flavor network bandwidth';
COMMENT ON COLUMN stackit_all.net_peak_gbitps IS 'Always NULL (see net_gbitps)';
COMMENT ON COLUMN stackit_all.storage_gb IS 'Local NVMe disk in GB (NULL when the flavor has no local disk)';
COMMENT ON COLUMN stackit_all.storage_count IS 'Number of local disks';
COMMENT ON COLUMN stackit_all.storage_is_ssd IS 'Whether local storage is SSD';
COMMENT ON COLUMN stackit_all.storage_is_nvme IS 'Whether local storage is directly-attached NVMe';
COMMENT ON COLUMN stackit_all.ebs_iops IS 'Always NULL -- STACKIT block-storage throughput is per-volume, not per-flavor';
COMMENT ON COLUMN stackit_all.ebs_gbitps IS 'Always NULL (see ebs_iops)';
COMMENT ON COLUMN stackit_all.ebs_peak_iops IS 'Always NULL (see ebs_iops)';
COMMENT ON COLUMN stackit_all.ebs_peak_gbitps IS 'Always NULL (see ebs_iops)';
COMMENT ON COLUMN stackit_all.accelerators IS 'Number of attached GPUs (from the .gN flavor suffix)';
COMMENT ON COLUMN stackit_all.accelerator_model IS 'GPU model (from the docs GPU table)';
COMMENT ON COLUMN stackit_all.accelerator_gib IS 'Total GPU memory in GiB';
COMMENT ON COLUMN stackit_all.is_current IS 'Whether the flavor is current (not deprecated / not a deprecated CPU generation)';
COMMENT ON COLUMN stackit_all.storage_read_iops IS 'Always NULL -- not published';
COMMENT ON COLUMN stackit_all.storage_write_iops IS 'Always NULL -- not published';
COMMENT ON COLUMN stackit_all.release_year IS 'Always NULL -- not published';
"""


def write_duckdb(rows, burst_families, out_path):
    con = duckdb.connect(out_path)
    cols_ddl = ", ".join(f'"{n}" {t}' for n, t in COLUMNS)
    for v in ("stackit_family", "stackit_accel", "stackit_burst", "stackit"):
        con.execute(f"drop view if exists {v}")
    con.execute("drop table if exists stackit_all cascade")
    con.execute(f"create table stackit_all ({cols_ddl})")
    con.executemany(
        f"insert into stackit_all values ({', '.join(['?'] * len(COLUMNS))})", rows
    )
    con.execute(views_sql(burst_families))
    counts = {
        v: con.execute(f"select count(*) from {v}").fetchone()[0]
        for v in ("stackit_all", "stackit", "stackit_family", "stackit_accel", "stackit_burst")
    }
    con.close()
    print("Wrote " + out_path)
    for v, c in counts.items():
        print(f"  {v}: {c}")


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--output", default=os.path.join(HERE, "cloudspecs.duckdb"),
                    help="DuckDB file to write the stackit_all table + views into")
    ap.add_argument("--region", default=DEFAULT_REGION,
                    help="single region to price (default eu01)")
    ap.add_argument("--eur-usd", type=float, default=None,
                    help="EUR->USD rate (default: live ECB reference rate)")
    ap.add_argument("--refresh", action="store_true",
                    help="re-download the cached price list + docs page")
    args = ap.parse_args()

    rate, src = eur_to_usd(args.eur_usd)
    print(f"EUR->USD = {rate} ({src})")

    flavors = fetch_prices(args.region, args.refresh)
    cpu_by_fam, gpu_by_fam, disk_by_flavor = parse_docs(args.refresh)

    print("Building rows ...")
    rows, burst = build_rows(flavors, rate, cpu_by_fam, gpu_by_fam, disk_by_flavor)
    write_duckdb(rows, burst, args.output)


if __name__ == "__main__":
    sys.exit(main())
