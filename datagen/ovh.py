#!/usr/bin/env python3
"""
Get OVHcloud Public Cloud instance data.

Standalone OVHcloud counterpart to build.py / gcp.py / azure.py / stackit.py. Writes an
`ovh_all` table + views into DuckDB with columns matching aws_all exactly, so all five
clouds (AWS, GCP, Azure, STACKIT, OVHcloud) are directly comparable.

ONE public source, no auth: the OVHcloud order catalog

    https://api.ovh.com/1.0/order/catalog/public/cloud?ovhSubsidiary=FR

Each instance flavor is an `addons` entry (`product == "publiccloud-instance"`) whose
`blobs.technical` carries the full spec -- cpu vCores, memory, public-network bandwidth
(Mbps), local disk(s) with IOPS, and GPU model/memory/count -- and whose `pricings`
carries the hourly ("consumption") price. We take the base `<flavor>.consumption` plan
(the standard hourly price), skip the Windows (`win-*`) variants, and read Linux prices.

Prices are published in the subsidiary's currency (EUR for FR/DE); price_hour is
converted to USD (to match the other clouds) at the live ECB reference rate,
overridable with --eur-usd.

Derived / static: family (flavor name minus the "-<size>" suffix), category (from the
catalog "brick"/subtype), release_year and the standard-flavor CPU model (curated
per-family tables -- the catalog reports "vCore" for everything except the bare-metal
flavors).

Output objects (mirroring the AWS/GCP/Azure/STACKIT builds):
  ovh_all    every flavor, all 27 aws_all columns.
  ovh        comparable slice: current, priced, dedicated-resources (non-shared,
             non-sandbox), non-GPU.
  ovh_family one representative flavor per family (net-efficiency window like aws_family).
  ovh_accel  GPU flavors, with the GPU model.
  ovh_burst  sandbox / shared-vCore flavors, incl. the Discovery (d2) range -- OVH's cheap
             tier.

Notes vs AWS: cores is the physical-core count. OVH publishes vCores (= vcpus); on its
x86_64 hosts a vCore is one hardware thread of a 2-way-hyperthreaded (SMT) core, so
cores = vcpus / 2. Bare-metal flavors are the exception -- the catalog reports their real
physical cores and threads directly (e.g. 16C/32T), so vcpus = threads and cores = cores.
processor_model is the CPU model where the catalog names one (bare metal) or a curated
per-family value, else null (standard flavors report a generic "vCore"). release_year is
approximate (curated per-family GA year). ebs_* is null -- OVH block-storage throughput
is per-volume, not per-flavor. net_gbitps is the guaranteed public-bandwidth level (no
separate peak, so net_peak == net).
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_SUBSIDIARY = "FR"  # EUR catalog; the consumption price is uniform across EUR subs
CATALOG_URL = "https://api.ovh.com/1.0/order/catalog/public/cloud"
PRICE_DIVISOR = 1e8        # catalog `price` is in micro-units: 7090000 -> 0.0709 EUR/h

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
FALLBACK_EUR_USD = 1.14    # ~mid-2026; used only if ECB is unreachable (or via --eur-usd)

WORK = os.path.join(HERE, "work", "ovh")

# category from the catalog commercial "brick" subtype.
CATEGORY_BY_SUBTYPE = {
    "general-purpose": "General purpose",
    "cpu": "Compute optimized",
    "ram": "Memory optimized",
    "iops": "Storage optimized",
    "metal": "Bare metal",
    "discovery": "General purpose",
}

# The catalog abstracts standard-flavor CPUs as generic "vCore"; these are curated from
# OVHcloud announcements + third-party probes (bare-metal flavors carry a real model in
# the catalog and win over this table). Keyed by family. Vendor is confident; exact SKUs
# only where well-observed. null-safe: unknown families (eg/hg/sp/vps-ssd) -> null.
_CPU_BY_FAMILY = {
    # Standard Intel "Gen 2" (exact SKU masked by the hypervisor)
    "b2": "Intel Xeon", "c2": "Intel Xeon", "r2": "Intel Xeon",
    "i1": "Intel Xeon", "d2": "Intel Xeon", "s1": "Intel Xeon",
    # Standard AMD "Gen 3" (b3 observed on Milan/Genoa; c3/r3 same platform)
    "b3": "AMD EPYC (Milan/Genoa)", "c3": "AMD EPYC (Milan/Genoa)",
    "r3": "AMD EPYC (Milan/Genoa)",
    # GPU hosts
    "t1": "Intel Xeon Gold (Cascade Lake)", "t1-le": "Intel Xeon Gold (Cascade Lake)",
    "t2": "Intel Xeon Gold (Cascade Lake)", "t2-le": "Intel Xeon Gold (Cascade Lake)",
    "rtx5000": "Intel Xeon W-3235",
    "a10": "AMD EPYC 9554 (Genoa)", "h100": "AMD EPYC 9354 (Genoa)",
    "l4": "AMD EPYC 9454 (Genoa)", "l40s": "AMD EPYC 9124 (Genoa)",
    "a100": "AMD EPYC (Genoa)", "h200": "AMD EPYC (Genoa)",
    "g1": "Intel Xeon", "g2": "Intel Xeon", "g3": "Intel Xeon",
}

# Approximate general-availability year per family (OVH exposes no GA date). Curated from
# OVHcloud blog/roadmap/press; anchors are well-documented (Gen 3 b3/c3/r3 = 2023 on AMD
# EPYC; h100/l4/l40s = 2023, a10 = 2024, h200 = 2025). Others are estimates.
_YEAR_BY_FAMILY = {
    "s1": 2015.0, "b2": 2016.0, "c2": 2016.0, "r2": 2016.0, "i1": 2019.0, "d2": 2021.0,
    "b3": 2023.0, "c3": 2023.0, "r3": 2023.0,
    "bm-s1": 2023.0, "bm-m1": 2023.0, "bm-l1": 2023.0,
    "g1": 2018.0, "g2": 2018.0, "g3": 2018.0,
    "t1": 2019.0, "t1-le": 2019.0, "t2": 2020.0, "t2-le": 2020.0,
    "a100": 2023.0, "h100": 2023.0, "l4": 2023.0, "l40s": 2023.0,
    "a10": 2024.0, "rtx5000": 2024.0, "h200": 2025.0,
}


# --------------------------------------------------------------------------- #
# HTTP + caching
# --------------------------------------------------------------------------- #
def _get(url, timeout=90):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _cached(name, url, refresh):
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
# Catalog
# --------------------------------------------------------------------------- #
def fetch_catalog(subsidiary, refresh):
    print(f"Fetching OVHcloud public-cloud catalog (ovhSubsidiary={subsidiary}) ...")
    url = f"{CATALOG_URL}?ovhSubsidiary={urllib.parse.quote(subsidiary)}"
    data = json.loads(_cached(f"catalog-{subsidiary}.json", url, refresh))
    cur = (data.get("locale") or {}).get("currencyCode")
    print(f"  catalog {data.get('catalogId')} -- currency {cur}")
    return data, cur


# --------------------------------------------------------------------------- #
# Spec helpers
# --------------------------------------------------------------------------- #
def _family(name):
    """Family = flavor name minus its "-<size>" suffix (b2-7 -> b2, t1-le-45 -> t1-le)."""
    return re.sub(r"-\d+$", "", name)


def _cpu_topology(cpu):
    """(vcpus, cores) for a flavor's catalog `cpu` blob.

    Standard and GPU flavors report a generic "vCore" count in cpu.cores (cpu.type ==
    "vCore"). Those vCores are hardware threads on OVHcloud's x86_64 hosts, which run 2-way
    hyperthreading (SMT) -- so the flavor's vCPU count is that vCore count and its physical-
    core count is half of it (ceil: a lone thread still occupies a full core).

    Bare-metal flavors (cpu.type == "core") are the whole physical machine, and the catalog
    reports the real physical `cores` and total `threads` (also 2-way SMT, e.g. 16C/32T), so
    vcpus = threads and cores = cores directly.
    """
    if (cpu.get("type") or "").lower() == "core":
        cores = int(cpu.get("cores") or 0)
        vcpus = int(cpu.get("threads") or 0) or cores
        return vcpus, (cores or None)
    vcpus = int(cpu.get("cores") or 0)
    return vcpus, ((vcpus + 1) // 2 if vcpus else None)


def _processor(cpu_model, family):
    """Real catalog CPU model (bare metal) if given, else the curated per-family value."""
    if cpu_model and cpu_model != "vCore":
        if cpu_model.startswith("EPYC"):
            return "AMD " + cpu_model
        if cpu_model.startswith("Xeon"):
            return "Intel " + cpu_model
        return cpu_model
    return _CPU_BY_FAMILY.get(family)


def _accelerator(gpu):
    """(model, per_gpu_gib) from the catalog gpu blob, model normalized to a vendor."""
    model = gpu.get("model")
    if model and not re.match(r"(NVIDIA|AMD|Intel)", model):
        model = "NVIDIA " + model.replace("-", " ")
    return model, (gpu.get("memory") or {}).get("size")


def _storage(st):
    """(total_gb, disk_count, is_ssd, is_nvme, iops) from the catalog storage blob."""
    disks = (st or {}).get("disks") or []
    if not disks:
        return None, None, None, None, None
    total = sum((d.get("number") or 1) * (d.get("capacity") or 0) for d in disks)
    count = sum(d.get("number") or 1 for d in disks)
    techs = {(d.get("technology") or "").upper() for d in disks if d.get("technology")}
    if techs:
        is_ssd = bool(techs & {"SSD", "NVME"})
        is_nvme = "NVME" in techs
    else:
        is_ssd = is_nvme = None          # d2/discovery disks omit the technology
    iops = max((d.get("iops") or 0) for d in disks) or None
    return total or None, count, is_ssd, is_nvme, iops


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


def build_rows(catalog, rate):
    """One row per Linux base-consumption instance flavor. Returns (rows, burst_families)
    where burst_families are the non-guaranteed (sandbox / shared-vCore) families."""
    addons = catalog["addons"]
    rows = []
    burst_families = set()
    for a in sorted(addons, key=lambda x: x.get("invoiceName") or ""):
        name = a.get("invoiceName") or ""
        if a.get("product") != "publiccloud-instance":
            continue
        if a.get("planCode") != f"{name}.consumption":   # base hourly plan only
            continue
        if name.startswith("win-"):                       # Linux prices only
            continue
        blobs = a.get("blobs") or {}
        tech = blobs.get("technical") or {}
        comm = blobs.get("commercial") or {}
        cpu, mem = tech.get("cpu"), tech.get("memory")
        if not cpu or not mem:                            # spec-less catalog stubs
            continue
        if not a.get("pricings"):
            continue

        fam = _family(name)
        brick = comm.get("brick")
        gpu = tech.get("gpu")
        vcpus, cores = _cpu_topology(cpu)
        bw = ((tech.get("bandwidth") or {}).get("level") or 0) / 1000.0 or None
        s_gb, s_cnt, s_ssd, s_nvme, s_iops = _storage(tech.get("storage"))

        if gpu:
            category = "GPU"
        else:
            category = CATEGORY_BY_SUBTYPE.get(comm.get("brickSubtype")) or "General purpose"
        # non-guaranteed (sandbox / shared vCore) families -> the "burst" tier. The Discovery
        # (d2) range is shared-resources too -- the catalog still tags it guaranteed-resources,
        # but OVH markets it as shared -- so catch it via its "discovery" brick subtype.
        if not gpu and (brick != "guaranteed-resources"
                        or comm.get("brickSubtype") == "discovery"):
            burst_families.add(fam)

        acc_model, acc_per = _accelerator(gpu) if gpu else (None, None)
        n_gpu = int(gpu.get("number") or 0) if gpu else 0

        rows.append((
            name,                                          # instance
            fam,                                           # family
            category,                                      # category
            round(a["pricings"][0]["price"] / PRICE_DIVISOR * rate, 6),  # price_hour USD
            float(mem.get("size") or 0),                   # ram_gib
            vcpus,                                          # vcpus
            float(vcpus),                                  # vcpus_base
            cores,                                         # cores (physical; vCore = HT thread)
            _processor(cpu.get("model"), fam),             # processor_model
            "x86_64",                                      # arch (all OVH flavors are x86)
            bw, bw,                                         # net_gbitps / net_peak_gbitps
            s_gb,                                          # storage_gb
            s_cnt,                                         # storage_count
            s_ssd,                                         # storage_is_ssd
            s_nvme,                                        # storage_is_nvme
            None, None, None, None,                        # ebs_iops/gbitps/peak_iops/peak_gbitps
            n_gpu,                                          # accelerators
            acc_model,                                     # accelerator_model
            (n_gpu * acc_per) if (n_gpu and acc_per) else None,  # accelerator_gib
            "legacy" not in blobs.get("tags", []),         # is_current
            s_iops, s_iops,                                # storage_read_iops / write_iops
            _YEAR_BY_FAMILY.get(fam),                      # release_year
        ))
    n_gpu = sum(1 for r in rows if r[2] == "GPU")
    print(f"  {len(rows)} Linux flavors ({n_gpu} GPU); sandbox/shared families: "
          f"{', '.join(sorted(burst_families))}")
    return rows, sorted(burst_families)


def views_sql(burst_families):
    burst = ", ".join(f"'{f}'" for f in burst_families) or "''"
    return f"""
create view ovh as
  select instance, family, category, price_hour, release_year, ram_gib, vcpus, cores, processor_model, arch, net_gbitps, net_peak_gbitps, storage_gb, storage_count, storage_read_iops, storage_write_iops, storage_is_ssd, storage_is_nvme, ebs_iops, ebs_gbitps, ebs_peak_iops, ebs_peak_gbitps
  from ovh_all
  where is_current
  and price_hour is not null
  and accelerators = 0
  and family not in ({burst});
create view ovh_family as
  select * from ovh join
  (select case when two.instance is null then one.instance
              when ((two.net_gbitps/two.price_hour)/(one.net_gbitps/one.price_hour) > 1.1) then two.instance
              else one.instance end as instance
  from (select * from (select *, row_number() over (partition by family order by vcpus desc) r from ovh) where r = 1) one
  left join (select * from (select *, row_number() over (partition by family order by vcpus desc) r from ovh) where r = 2) two
  using (family)) using (instance);
create view ovh_accel as
  select * from ovh_all
  where category = 'GPU' and accelerator_model is not null;
create view ovh_burst as
  select * from ovh_all where family in ({burst});
COMMENT ON COLUMN ovh_all.instance IS 'OVHcloud instance flavor name (e.g. b3-8)';
COMMENT ON COLUMN ovh_all.price_hour IS 'Standard-region Linux hourly on-demand price in USD (catalog consumption price, converted from EUR at the ECB reference rate)';
COMMENT ON COLUMN ovh_all.family IS 'Flavor family: flavor name minus the "-<size>" suffix (b3-8 -> b3, t1-le-45 -> t1-le)';
COMMENT ON COLUMN ovh_all.category IS 'Category from the catalog brick subtype (General purpose, Compute optimized, Memory optimized, Storage optimized, Bare metal, GPU)';
COMMENT ON COLUMN ovh_all.ram_gib IS 'Amount of main memory in GiB';
COMMENT ON COLUMN ovh_all.vcpus IS 'Number of vCPUs (OVH vCores = hardware threads; bare-metal flavors expose all host threads)';
COMMENT ON COLUMN ovh_all.vcpus_base IS 'Same as vcpus';
COMMENT ON COLUMN ovh_all.cores IS 'Physical cores: OVH vCores are hardware threads on 2-way-hyperthreaded x86_64 hosts, so cores = vcpus/2 (ceil); bare-metal flavors report real physical cores (catalog gives cores + threads, e.g. 16C/32T)';
COMMENT ON COLUMN ovh_all.processor_model IS 'CPU model where the catalog names one (bare metal) or a curated per-family value; NULL for standard flavors (catalog reports generic "vCore")';
COMMENT ON COLUMN ovh_all.arch IS 'Processor architecture (all OVH public-cloud flavors are x86_64)';
COMMENT ON COLUMN ovh_all.net_gbitps IS 'Guaranteed public-network bandwidth in Gbit/s';
COMMENT ON COLUMN ovh_all.net_peak_gbitps IS 'Same as net_gbitps (OVH publishes one bandwidth level)';
COMMENT ON COLUMN ovh_all.storage_gb IS 'Total local disk in GB (sum over local disks; NULL when the flavor has no local disk)';
COMMENT ON COLUMN ovh_all.storage_count IS 'Number of local disks';
COMMENT ON COLUMN ovh_all.storage_is_ssd IS 'Whether local storage is SSD/NVMe';
COMMENT ON COLUMN ovh_all.storage_is_nvme IS 'Whether local storage is directly-attached NVMe';
COMMENT ON COLUMN ovh_all.ebs_iops IS 'Always NULL -- OVH block-storage throughput is per-volume, not per-flavor';
COMMENT ON COLUMN ovh_all.ebs_gbitps IS 'Always NULL (see ebs_iops)';
COMMENT ON COLUMN ovh_all.ebs_peak_iops IS 'Always NULL (see ebs_iops)';
COMMENT ON COLUMN ovh_all.ebs_peak_gbitps IS 'Always NULL (see ebs_iops)';
COMMENT ON COLUMN ovh_all.accelerators IS 'Number of attached GPUs';
COMMENT ON COLUMN ovh_all.accelerator_model IS 'GPU model (from the catalog)';
COMMENT ON COLUMN ovh_all.accelerator_gib IS 'Total GPU memory in GiB';
COMMENT ON COLUMN ovh_all.is_current IS 'Whether the flavor is current (not tagged "legacy" in the catalog)';
COMMENT ON COLUMN ovh_all.storage_read_iops IS 'Local disk IOPS (catalog gives one value; NULL for NVMe flavors, which omit it)';
COMMENT ON COLUMN ovh_all.storage_write_iops IS 'Same as storage_read_iops (catalog gives one IOPS value)';
COMMENT ON COLUMN ovh_all.release_year IS 'Approximate family GA year (curated; OVH publishes no per-flavor GA date)';
"""


def write_duckdb(rows, burst_families, out_path):
    con = duckdb.connect(out_path)
    cols_ddl = ", ".join(f'"{n}" {t}' for n, t in COLUMNS)
    for v in ("ovh_family", "ovh_accel", "ovh_burst", "ovh"):
        con.execute(f"drop view if exists {v}")
    con.execute("drop table if exists ovh_all cascade")
    con.execute(f"create table ovh_all ({cols_ddl})")
    con.executemany(
        f"insert into ovh_all values ({', '.join(['?'] * len(COLUMNS))})", rows
    )
    con.execute(views_sql(burst_families))
    counts = {
        v: con.execute(f"select count(*) from {v}").fetchone()[0]
        for v in ("ovh_all", "ovh", "ovh_family", "ovh_accel", "ovh_burst")
    }
    con.close()
    print("Wrote " + out_path)
    for v, c in counts.items():
        print(f"  {v}: {c}")


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--output", default=os.path.join(HERE, "cloudspecs.duckdb"),
                    help="DuckDB file to write the ovh_all table + views into")
    ap.add_argument("--subsidiary", default=DEFAULT_SUBSIDIARY,
                    help="OVH catalog subsidiary / currency (EUR subs: FR, DE, ...)")
    ap.add_argument("--eur-usd", type=float, default=None,
                    help="EUR->USD rate (default: live ECB reference rate)")
    ap.add_argument("--refresh", action="store_true",
                    help="re-download the cached catalog")
    args = ap.parse_args()

    catalog, currency = fetch_catalog(args.subsidiary, args.refresh)
    if currency != "EUR":
        raise SystemExit(f"subsidiary {args.subsidiary} bills in {currency}; use a EUR "
                         f"subsidiary (FR, DE, ES, IT, PL, ...) so prices convert to USD")
    rate, src = eur_to_usd(args.eur_usd)
    print(f"EUR->USD = {rate} ({src})")

    print("Building rows ...")
    rows, burst = build_rows(catalog, rate)
    write_duckdb(rows, burst, args.output)


if __name__ == "__main__":
    sys.exit(main())
