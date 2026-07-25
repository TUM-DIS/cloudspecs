#!/usr/bin/env python3
"""
Get Scaleway Instance (cloud VM) data.

Standalone Scaleway counterpart to build.py / gcp.py / azure.py / stackit.py / ovh.py /
oracle.py / hetzner.py. Writes a `scaleway_all` table + views into DuckDB with columns
matching aws_all exactly, so all eight clouds are directly comparable.

TWO public sources, no auth:

  1. Instance API   https://api.scaleway.com/instance/v1/zones/<zone>/products/servers
     The type universe: every commercial type offered in a zone with its vCPU count, RAM,
     architecture, internet/private-network bandwidth, Block Storage bandwidth, GPU
     model/memory and end-of-service flag.
  2. Product Catalog API
     https://api.scaleway.com/product-catalog/v2alpha1/public-catalog/products
     Scaleway's published price list (`product_category=Instance`, `/compute/...` SKUs):
     price with an explicit currency code, the exact CPU model, whether the vCPUs are
     shared or dedicated, the commercial range and the GA/retired status.

Prices are published in EUR; price_hour is converted to USD (to match the other clouds) at
the live ECB reference rate, overridable with --eur-usd. The catalog price wins where a
type is listed there; the ~30 mid-sized types the catalog does not carry yet fall back to
the Instance API's `hourly_price` (the two agree on every other current type). Prices are
uniform across Scaleway's zones except fr-par-3 (~+50%), so the Paris reference below is
also the low-price reference: fr-par-1, plus fr-par-2 for the GPU types only offered there.

Derived / static: family (type name minus its size suffix), category (from the commercial
range, split into the usual compute/memory buckets), cores (physical, inferred from SMT)
and release_year (curated per family -- neither API publishes a GA date).

Output objects (mirroring the other builds):
  scaleway_all    every Instance type, all 27 aws_all columns.
  scaleway        comparable slice: current, priced, dedicated-vCPU, non-GPU -- the
                  shared-vCPU ranges are oversubscribed and excluded, like the other
                  clouds' burst tiers.
  scaleway_family one representative type per family (net-efficiency window like aws_family).
  scaleway_accel  GPU types, with the GPU model.
  scaleway_shared the shared-vCPU ranges (STARDUST1 / PLAY2 / DEV1 / GP1 / PRO2 / BASIC*)
                  -- Scaleway's oversubscribed tier.

Notes vs AWS: storage_* is always null -- Scaleway bills every volume (Local SSD, Scratch,
Block) separately per GB, so no storage capacity is included in an Instance's price.
ebs_gbitps is the type's Block Storage bandwidth cap (a real per-type limit, unlike the
other European clouds); ebs_*iops is null (Block Storage IOPS are a per-volume class).
net_gbitps is the private-network (NIC) bandwidth, the figure comparable to the other
clouds' network performance, and net_peak_gbitps repeats it (Scaleway has no burst tier).
Scaleway publishes a second, lower cap for public-internet traffic over that same NIC on
the small COMPUTE3/MEMORY3/STANDARD3 sizes (0.5-2 instead of 4 Gbit/s); that cap is not in
the schema, matching AWS and GCP, whose internet caps are likewise absent from their
net_gbitps. cores is inferred from the CPU's SMT: a Scaleway vCPU is one CPU
thread, so cores = vcpus/2 on the 2-way-SMT x86 hosts and cores = vcpus on the ARM
(Ampere) and Atom hosts. vcpus_base == vcpus -- shared vCPUs run at full speed (no
throttled baseline); they are flagged via scaleway_shared, not a fractional baseline.
Windows (POP2-*-WIN) types are skipped, like the other clouds' Linux-only prices.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))

INSTANCE_URL = ("https://api.scaleway.com/instance/v1/zones/{zone}/products/servers"
                "?per_page=100&page={page}")
CATALOG_URL = ("https://api.scaleway.com/product-catalog/v2alpha1/public-catalog/products"
               "?product_category=Instance&page_size=1000&page={page}")
# Paris reference: fr-par-1 carries every range but the newest GPUs, fr-par-2 adds those.
# Prices are identical in both (and in nl-ams-*/pl-waw-*); only fr-par-3 costs ~50% more.
DEFAULT_ZONES = "fr-par-1,fr-par-2"

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
FALLBACK_EUR_USD = 1.14    # ~mid-2026; used only if ECB is unreachable (or via --eur-usd)

WORK = os.path.join(HERE, "work", "scaleway")

# Category from the commercial range: Scaleway's own "Specialized" range mixes compute-,
# memory- and network-optimized families, so it is split into the vocabulary the other
# clouds use. Everything else (General Purpose + Development ranges) is general purpose.
_CATEGORY_BY_FAMILY = {
    "POP2-HC": "Compute optimized", "COMPUTE3-X": "Compute optimized",
    "POP2-HM": "Memory optimized", "MEMORY3-X": "Memory optimized",
    "POP2-HN": "Network optimized",
}

# The catalog names an exact CPU model; POP2-HN is the one range it leaves blank ("Not
# specified" in the datasheet), so its model comes from the Specialized-range doc page
# (https://www.scaleway.com/en/docs/instances/reference-content/specialized/).
_CPU_BY_FAMILY = {"POP2-HN": "AMD EPYC 7543"}

# Microarchitecture per CPU model, appended to processor_model like the OVH/Oracle builds
# do, so cross-cloud queries can group by CPU generation.
_UARCH = {
    "AMD EPYC 7281": "Naples", "AMD EPYC 7401P": "Naples",
    "AMD EPYC 7413": "Milan", "AMD EPYC 7543": "Milan",
    "AMD EPYC 9334": "Genoa", "AMD EPYC 9555P": "Turin",
    "Intel Xeon Gold 6148": "Skylake",
    "Intel Xeon Platinum 8452Y": "Sapphire Rapids",
    "Intel Xeon 6 6776P": "Granite Rapids",
    "Intel Atom C3855": "Denverton",
}

# General-availability year per family, as year + (month-1)/12. Neither API publishes a GA
# date, so these are curated from Scaleway's public changelog
# (https://www.scaleway.com/en/changelog/) where it announced the range, and otherwise from
# the commit that introduced the range to the public docs
# (github.com/scaleway/docs-content) -- a lagging bound, hence approximate. The retired
# VC1/X64/START1/RENDER ranges predate both and are rough.
_YEAR_BY_FAMILY = {
    "VC1": 2015.0,          # approximate -- Scaleway's first x86 cloud servers
    "X64": 2016.0,          # approximate
    "START1": 2017.0,       # approximate
    "DEV1": 2019.17,        # 2019-03, announced together with GP1
    "GP1": 2019.17,         # 2019-03
    "RENDER": 2019.5,       # approximate -- the P100 GPU offer
    "PRO2": 2022.42,        # changelog 2022-06-15
    "PLAY2": 2022.67,       # changelog 2022-09-06
    "POP2": 2023.42,        # changelog 2023-06-01 (production-optimized)
    "POP2-HC": 2023.42,     # changelog 2023-06-01 (workload-optimized)
    "POP2-HM": 2023.42,     # changelog 2023-06-01 (workload-optimized)
    "STARDUST1": 2023.67,   # changelog 2023-09-15
    "H100": 2023.75,        # changelog 2023-10-06 (H100 PCIe)
    "POP2-HN": 2024.0,      # approximate -- documented before 2025-02
    "L4": 2024.17,          # changelog 2024-03-12
    "L40S": 2024.25,        # changelog 2024-04-23
    "H100-SXM": 2025.25,    # changelog 2025-04-18
    "BASIC2-A": 2025.92,    # docs 2025-12-03
    "B300-SXM": 2026.08,    # datasheet 2026-02-06
    "STANDARD2-A": 2026.17,  # datasheet 2026-04-09, absent 2026-02-06
    "BASIC3-X": 2026.17,    # docs 2026-03-10
    "STANDARD3-X": 2026.17,  # docs 2026-03-10
    "COMPUTE3-X": 2026.25,  # datasheet 2026-04-09
    "MEMORY3-X": 2026.25,   # datasheet 2026-04-09
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
# Sources
# --------------------------------------------------------------------------- #
def fetch_types(zones, refresh):
    """Instance types offered in `zones`, keyed by name. Earlier zones win (same price)."""
    types = {}
    for zone in zones:
        print(f"Fetching Scaleway Instance types (zone={zone}) ...")
        servers, page = {}, 1
        while True:
            data = json.loads(_cached(f"servers-{zone}-{page}.json",
                                      INSTANCE_URL.format(zone=zone, page=page), refresh))
            batch = data.get("servers") or {}
            if not batch:
                break
            servers.update(batch)
            page += 1
        print(f"  {len(servers)} types")
        for name, spec in servers.items():
            types.setdefault(name, spec)
    return types


def _key(name):
    """Match key for a type name: the catalog spells a few legacy ranges with extra
    dashes (VC1L -> "VC1-L"), and neither namespace has a collision without them."""
    return re.sub(r"[^A-Z0-9]", "", name.upper())


def fetch_catalog(zones, refresh):
    """Instance products from the public price catalog, keyed by (type key, zone).

    Instance types are the `/compute/...` SKUs -- the rest of the Instance category is
    volumes and snapshots, plus a few retired `/instance/server/...` stubs. A product can
    appear more than once per zone when an offer was re-issued; the generally-available
    entry with hardware specs wins over a retired stub.
    """
    print("Fetching Scaleway product catalog ...")
    products, page = [], 1
    while True:
        data = json.loads(_cached(f"catalog-{page}.json",
                                  CATALOG_URL.format(page=page), refresh))
        products += data.get("products") or []
        if len(products) >= int(data.get("total_count") or 0):
            break
        page += 1
    out = {}
    for p in products:
        if not p["sku"].startswith("/compute/"):
            continue
        zone = (p.get("locality") or {}).get("zone")
        if zone not in zones:
            continue
        key = (_key(p["product"]), zone)
        cur = out.get(key)
        if cur is None or (_rank(p) > _rank(cur)):
            out[key] = p
    print(f"  {len(products)} Instance products, {len(out)} priced type/zone pairs "
          f"in {', '.join(zones)}")
    return out


def _rank(p):
    """Prefer a generally-available catalog entry that carries hardware specs."""
    hw = bool((p.get("properties") or {}).get("hardware"))
    return (p.get("status") == "general_availability", hw)


def catalog_price_eur(product):
    """Hourly EUR price of a catalog product (GPU types are metered per minute). Retired
    offers can carry a dead 0-price entry; those return None and fall back."""
    price = (product.get("price") or {}).get("retail_price") or {}
    currency = price.get("currency_code")
    if currency and currency != "EUR":
        raise SystemExit(f"{product['sku']} is priced in {currency}; this build converts "
                         f"EUR prices to USD")
    per_unit = float(price.get("units") or 0) + float(price.get("nanos") or 0) / 1e9
    uom = product.get("unit_of_measure") or {}
    factor = {"hour": 1.0, "minute": 60.0}.get(uom.get("unit"))
    if factor is None or not per_unit:
        return None
    return per_unit * factor / float(uom.get("size") or 1)


# --------------------------------------------------------------------------- #
# Spec helpers
# --------------------------------------------------------------------------- #
_SIZE_SUFFIXES = (
    r"-?\d+C-\d+G",                                 # POP2-2C-8G, BASIC2-A12C-24G (keeps
                                                    # the -A / -X architecture letter)
    r"-\d+-\d+G",                                   # L4-1-24G, H100-SXM-2-80G
    r"-(XXS|XS|S|M|L|XL|PICO|NANO|MICRO)",          # DEV1-S, PRO2-XXS, PLAY2-PICO
    r"-\d+GB",                                      # X64-15GB
    r"-\d+",                                        # POP2-HN-3
)


def _family(name):
    """Family = type name minus its size suffix (POP2-HC-8C-16G -> POP2-HC, DEV1-S ->
    DEV1, BASIC2-A4C-8G -> BASIC2-A, H100-SXM-2-80G -> H100-SXM, VC1L -> VC1)."""
    for suffix in _SIZE_SUFFIXES:
        fam = re.sub(rf"{suffix}$", "", name)
        if fam != name:
            return fam
    return re.sub(r"(?<=\d)[SML]$", "", name)       # VC1S / VC1M / VC1L -> VC1


def _processor(cpu_type, fam):
    """Catalog CPU model, normalized: drop the trademark sign, the clock and Scaleway's
    "or equivalent" qualifier, then append the microarchitecture."""
    model = cpu_type or _CPU_BY_FAMILY.get(fam)
    if not model:
        return None
    model = model.replace("™", "")
    model = re.sub(r"\s*\([0-9.]+\s*GHz\)", "", model)
    model = re.sub(r"\s+or equivalent$", "", model)
    model = re.sub(r"\s+", " ", model).strip()
    if model == "Ampere M128-30":                   # the catalog's short form
        model = "Ampere Altra Max M128-30"
    uarch = _UARCH.get(model)
    return f"{model} ({uarch})" if uarch else model


def _cores(vcpus, proc, arch):
    """Physical cores inferred from the CPU's threads-per-core. A Scaleway vCPU is one CPU
    thread, so cores = vcpus / 2 on the 2-way-SMT x86 hosts (AMD EPYC, Intel Xeon); the ARM
    hosts (Ampere Altra Max) and the legacy Atom hosts have no SMT, so cores = vcpus."""
    no_smt = arch == "arm64" or (proc and ("Ampere" in proc or "Atom" in proc))
    return vcpus if no_smt else (vcpus + 1) // 2    # ceil: 3 threads span 2 cores


def _accelerator(spec):
    """(count, model, total_gib) from the Instance API's gpu / gpu_info fields."""
    count = int(spec.get("gpu") or 0)
    info = spec.get("gpu_info") or {}
    if not count or not info:
        return count, None, None
    # H100-PCIe / H100-SXM / B300-SXM name the form factor; drop it so the model matches
    # the other clouds' rows (NVIDIA H100, NVIDIA B300, ...).
    name = re.sub(r"-(PCIe|SXM)$", "", info.get("gpu_name") or "")
    vendor = info.get("gpu_manufacturer") or "NVIDIA"
    model = f"{vendor} Tesla {name}" if name == "P100" else f"{vendor} {name}"
    per_gpu = float(info.get("gpu_memory") or 0) / 2 ** 30
    return count, (model if name else None), (count * per_gpu if per_gpu else None)


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


def family_cpus(catalog):
    """CPU model and shared/dedicated flag per family, pooled over every cataloged type of
    that family. The catalog does not carry the mid-sized types of the newer ranges (and
    some retired ones), but a range is uniformly one CPU and one provisioning mode, so a
    sibling type answers for them."""
    out = {}
    for product in sorted(catalog.values(), key=lambda p: not _rank(p)[0]):  # GA first
        cpu = (product.get("properties", {}).get("hardware") or {}).get("cpu") or {}
        fam = out.setdefault(_family(product["product"]), {"type": None, "shared": None})
        fam["type"] = fam["type"] or cpu.get("type")
        if fam["shared"] is None:
            fam["shared"] = cpu.get("shared")
    return out


def build_rows(types, catalog, zones, rate):
    """One 27-column row per Linux Instance type. Returns (rows, shared_families)."""
    by_family = family_cpus(catalog)
    rows, shared, fallback, no_price, unknown = [], set(), [], [], []
    for name, spec in sorted(types.items()):
        if name.endswith("-WIN"):                   # Linux prices only
            continue
        fam = _family(name)
        product = next((catalog[(_key(name), z)] for z in zones
                        if (_key(name), z) in catalog), None)
        cpu = ((product or {}).get("properties", {}).get("hardware") or {}).get("cpu") or {}
        sibling = by_family.get(fam, {})

        eur = catalog_price_eur(product) if product else None
        if eur is None:
            eur = spec.get("hourly_price")           # types the catalog does not carry
            (fallback if eur else no_price).append(name)

        is_shared = cpu.get("shared")
        if is_shared is None:
            is_shared = sibling.get("shared")
        if is_shared is None:                        # a range nothing in the catalog covers
            is_shared = False
            unknown.append(name)
        if is_shared:
            shared.add(fam)

        vcpus = int(spec.get("ncpus") or 0)
        arch = spec.get("arch") or "x86_64"
        proc = _processor(cpu.get("type") or sibling.get("type"), fam)
        n_gpu, acc_model, acc_gib = _accelerator(spec)
        net = spec.get("network") or {}
        internal = (net.get("sum_internal_bandwidth") or 0) / 1e9
        block = (spec.get("block_bandwidth") or 0) * 8 / 1e9   # bytes/s -> Gbit/s
        retired = bool(spec.get("end_of_service")) or (
            product is not None and product.get("status") == "retired")

        rows.append((
            name,                                          # instance
            fam,                                           # family
            "GPU" if n_gpu else _CATEGORY_BY_FAMILY.get(fam, "General purpose"),  # category
            round(eur * rate, 6) if eur else None,         # price_hour (EUR -> USD)
            float(spec.get("ram") or 0) / 2 ** 30,         # ram_gib
            vcpus,                                          # vcpus
            float(vcpus),                                   # vcpus_base (full-speed vCPUs)
            _cores(vcpus, proc, arch),                      # cores (physical, inferred)
            proc,                                           # processor_model
            arch,                                           # arch
            internal or None,                               # net_gbitps (NIC)
            internal or None,                               # net_peak_gbitps (no burst tier)
            None, None, None, None,                         # storage_* (billed separately)
            None,                                           # ebs_iops (per-volume class)
            block or None,                                  # ebs_gbitps (Block Storage)
            None,                                           # ebs_peak_iops
            block or None,                                  # ebs_peak_gbitps
            n_gpu,                                          # accelerators
            acc_model,                                      # accelerator_model
            acc_gib,                                        # accelerator_gib
            not retired,                                    # is_current
            None, None,                                     # storage_read/write_iops
            _YEAR_BY_FAMILY.get(fam),                       # release_year
        ))
    n_gpu = sum(1 for r in rows if r[2] == "GPU")
    print(f"  {len(rows)} Linux types ({n_gpu} GPU); shared-vCPU families: "
          f"{', '.join(sorted(shared))}")
    if fallback:
        print(f"  {len(fallback)} types priced from the Instance API (not in the "
              f"catalog): {', '.join(fallback)}")
    if no_price:
        print(f"  WARNING: {len(no_price)} types without a price: {', '.join(no_price)}")
    if unknown:
        print(f"  WARNING: {len(unknown)} types whose range the catalog does not cover, "
              f"assumed dedicated-vCPU: {', '.join(unknown)}")
    return rows, sorted(shared)


def views_sql(shared_families):
    burst = ", ".join(f"'{f}'" for f in shared_families) or "''"
    return f"""
create view scaleway as
  select instance, family, category, price_hour, release_year, ram_gib, vcpus, cores, processor_model, arch, net_gbitps, net_peak_gbitps, storage_gb, storage_count, storage_read_iops, storage_write_iops, storage_is_ssd, storage_is_nvme, ebs_iops, ebs_gbitps, ebs_peak_iops, ebs_peak_gbitps
  from scaleway_all
  where is_current
  and price_hour is not null
  and accelerators = 0
  and family not in ({burst});
create view scaleway_family as
  select * from scaleway join
  (select case when two.instance is null then one.instance
              when ((two.net_gbitps/two.price_hour)/(one.net_gbitps/one.price_hour) > 1.1) then two.instance
              else one.instance end as instance
  from (select * from (select *, row_number() over (partition by family order by vcpus desc) r from scaleway) where r = 1) one
  left join (select * from (select *, row_number() over (partition by family order by vcpus desc) r from scaleway) where r = 2) two
  using (family)) using (instance);
create view scaleway_accel as
  select * from scaleway_all
  where category = 'GPU' and accelerator_model is not null;
create view scaleway_shared as
  select * from scaleway_all where family in ({burst});
COMMENT ON COLUMN scaleway_all.instance IS 'Scaleway Instance type name (e.g. POP2-8C-32G)';
COMMENT ON COLUMN scaleway_all.price_hour IS 'Paris-reference (fr-par-1/2) Linux hourly on-demand price in USD, compute only -- volumes and the flexible IPv4 are billed separately (converted from EUR at the ECB reference rate)';
COMMENT ON COLUMN scaleway_all.family IS 'Type family: type name minus its size suffix (POP2-HC-8C-16G -> POP2-HC, DEV1-S -> DEV1, H100-SXM-2-80G -> H100-SXM)';
COMMENT ON COLUMN scaleway_all.category IS 'Category from the commercial range (General purpose, Compute optimized, Memory optimized, Network optimized, GPU); Scaleway''s "Specialized" range is split into the compute/memory/network buckets';
COMMENT ON COLUMN scaleway_all.ram_gib IS 'Amount of main memory in GiB';
COMMENT ON COLUMN scaleway_all.vcpus IS 'Number of vCPUs (one vCPU is one CPU thread)';
COMMENT ON COLUMN scaleway_all.vcpus_base IS 'Same as vcpus (shared vCPUs run at full speed; the shared ranges are flagged via scaleway_shared)';
COMMENT ON COLUMN scaleway_all.cores IS 'Physical cores inferred from the CPU: a vCPU is one thread, so cores = vcpus/2 on the 2-way-SMT x86 hosts (AMD EPYC, Intel Xeon) and cores = vcpus on the ARM (Ampere Altra Max) and Atom hosts';
COMMENT ON COLUMN scaleway_all.processor_model IS 'CPU model from the product catalog with its microarchitecture appended; Scaleway qualifies the DEV1/GP1/STARDUST1 model as "or equivalent"';
COMMENT ON COLUMN scaleway_all.arch IS 'Processor architecture (x86_64 or arm64)';
COMMENT ON COLUMN scaleway_all.net_gbitps IS 'Private-network (NIC) bandwidth in Gbit/s, the figure comparable to network performance on the other clouds; Scaleway meters public-internet traffic over the same NIC at a lower cap on the small COMPUTE3/MEMORY3/STANDARD3 sizes (0.5-2 instead of 4 Gbit/s), which this column does not carry';
COMMENT ON COLUMN scaleway_all.net_peak_gbitps IS 'Same as net_gbitps (Scaleway has no burst tier)';
COMMENT ON COLUMN scaleway_all.storage_gb IS 'Always NULL -- Scaleway includes no storage in an Instance price: Local SSD, Scratch and Block volumes are all billed separately per GB';
COMMENT ON COLUMN scaleway_all.storage_count IS 'Always NULL (see storage_gb)';
COMMENT ON COLUMN scaleway_all.storage_is_ssd IS 'Always NULL (see storage_gb)';
COMMENT ON COLUMN scaleway_all.storage_is_nvme IS 'Always NULL (see storage_gb)';
COMMENT ON COLUMN scaleway_all.ebs_iops IS 'Always NULL -- Block Storage IOPS are a per-volume class (5k / 15k), not a per-type limit';
COMMENT ON COLUMN scaleway_all.ebs_gbitps IS 'Per-type Block Storage bandwidth cap in Gbit/s';
COMMENT ON COLUMN scaleway_all.ebs_peak_iops IS 'Always NULL (see ebs_iops)';
COMMENT ON COLUMN scaleway_all.ebs_peak_gbitps IS 'Same as ebs_gbitps (Scaleway publishes one cap)';
COMMENT ON COLUMN scaleway_all.accelerators IS 'Number of attached GPUs';
COMMENT ON COLUMN scaleway_all.accelerator_model IS 'GPU model (from the Instance API; the PCIe/SXM form factor is part of the instance name)';
COMMENT ON COLUMN scaleway_all.accelerator_gib IS 'Total GPU memory in GiB';
COMMENT ON COLUMN scaleway_all.is_current IS 'Whether the type is current (not end-of-service / retired)';
COMMENT ON COLUMN scaleway_all.storage_read_iops IS 'Always NULL (see storage_gb)';
COMMENT ON COLUMN scaleway_all.storage_write_iops IS 'Always NULL (see storage_gb)';
COMMENT ON COLUMN scaleway_all.release_year IS 'Approximate family GA year (curated from Scaleway''s changelog and docs history; neither API publishes a GA date)';
"""


def write_duckdb(rows, shared_families, out_path):
    con = duckdb.connect(out_path)
    cols_ddl = ", ".join(f'"{n}" {t}' for n, t in COLUMNS)
    for v in ("scaleway_family", "scaleway_accel", "scaleway_shared", "scaleway"):
        con.execute(f"drop view if exists {v}")
    con.execute("drop table if exists scaleway_all cascade")
    con.execute(f"create table scaleway_all ({cols_ddl})")
    con.executemany(
        f"insert into scaleway_all values ({', '.join(['?'] * len(COLUMNS))})", rows
    )
    con.execute(views_sql(shared_families))
    counts = {
        v: con.execute(f"select count(*) from {v}").fetchone()[0]
        for v in ("scaleway_all", "scaleway", "scaleway_family", "scaleway_accel",
                  "scaleway_shared")
    }
    con.close()
    print("Wrote " + out_path)
    for v, c in counts.items():
        print(f"  {v}: {c}")


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--output", default=os.path.join(HERE, "cloudspecs.duckdb"),
                    help="DuckDB file to write the scaleway_all table + views into")
    ap.add_argument("--zones", default=DEFAULT_ZONES,
                    help="comma-separated availability zones to price, first match wins "
                         "(default fr-par-1,fr-par-2; every zone but fr-par-3 has the "
                         "same prices)")
    ap.add_argument("--eur-usd", type=float, default=None,
                    help="EUR->USD rate (default: live ECB reference rate)")
    ap.add_argument("--refresh", action="store_true",
                    help="re-download the cached type list + price catalog")
    args = ap.parse_args()

    zones = [z.strip() for z in args.zones.split(",") if z.strip()]
    rate, src = eur_to_usd(args.eur_usd)
    print(f"EUR->USD = {rate} ({src})")

    types = fetch_types(zones, args.refresh)
    catalog = fetch_catalog(zones, args.refresh)
    print(f"  {len(types)} distinct types in {', '.join(zones)}")

    print("Building rows ...")
    rows, shared = build_rows(types, catalog, zones, rate)
    write_duckdb(rows, shared, args.output)


if __name__ == "__main__":
    sys.exit(main())
