#!/usr/bin/env python3
"""
Get Oracle Cloud Infrastructure (OCI) Compute instance data.

Standalone OCI counterpart to build.py / gcp.py / azure.py / stackit.py / ovh.py. Writes
an `oracle_all` table + views into DuckDB with columns matching aws_all exactly, so all
six clouds (AWS, GCP, Azure, STACKIT, OVHcloud, Oracle) are directly comparable.

ONE public source, no auth: the OCI cost-estimator product/price catalog

    https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/?currencyCode=USD

Like GCP, OCI has **no per-shape price**: compute is billed per *OCPU-hour* plus per
*memory-GB-hour* (plus, for GPU shapes, per *GPU-hour*), at a rate that depends only on
the shape family (E4, E5, A1, ...). Each family's OCPU / Memory / GPU rate is a catalog
SKU identified by a stable part number; we look those up live (already in USD) and
assemble every shape's hourly price as

    price_hour = ocpu_rate * OCPUs  +  mem_rate * RAM_GiB   (+ gpu_rate * nGPU)

The **shape specs** (which shapes exist, their OCPU range, memory ratio, CPU model,
network bandwidth, local NVMe, GPU model, release year) are curated in the CATALOG below
-- OCI publishes them only as documentation, not as a no-auth API. Prices stay live; the
specs are stable and sourced from docs.oracle.com/.../computeshapes.htm.

OCPU semantics (an OCPU is a billing unit, not a vCPU):
  * x86 (AMD/Intel):       1 OCPU = 1 physical core = 2 vCPUs (hyperthreads)
  * Arm Ampere Altra (A1): 1 OCPU = 1 core          = 1 vCPU  (no SMT)
  * Arm AmpereOne (A2/A4): 1 OCPU = 2 cores          = 2 vCPUs
So vcpus / cores are derived per family; price is always per OCPU.

Modern OCI shapes are **flexible** (you pick the OCPU count and memory), priced strictly
linearly with no premium for a custom ratio. We enumerate each flexible shape over a grid
of a doubling sequence of OCPU sizes (up to the family maximum) crossed with four memory
points -- 0.25x / 0.5x / 1x / 2x the family's default GB/OCPU ratio (16 GB/OCPU for x86 /
AmpereOne, 6 GB/OCPU for Altra A1). On x86 that is 2 / 4 / 8 / 16 GB per vCPU: a "compute"
point matching AWS c-series (2 GB/vCPU), a "general" point, OCI's console default, and a
"memory" point -- so the RAM axis stays comparable to the other clouds. Each row is named
`<shape>.<ocpus>-<GB>` (e.g. VM.Standard.E5.Flex.8-128). Fixed shapes (legacy
VM.Standard2/E2, bare metal, GPU, DenseIO) are taken as-is.

Output objects (mirroring the AWS/GCP/Azure/STACKIT/OVHcloud builds):
  oracle_all    every shape, all 27 aws_all columns.
  oracle        comparable slice: current, priced, non-GPU, non-bare-metal, non-micro.
  oracle_family one representative shape per family (net-efficiency window like aws_family).
  oracle_accel  GPU shapes, with the GPU model.
  oracle_shared  the free-tier micro shapes -- OCI's cheap/limited tier (OCI has no
                burstable-CPU family, so vcpus_base always equals vcpus).

Notes vs AWS: price is assembled (see above), so it is exact for the reference region but
carries no per-shape discount. ebs_* is null -- OCI block-volume performance is per-volume,
not per-shape. net_gbitps is the shape's max network bandwidth (scales with OCPU up to a
cap); no separate baseline, so net_peak == net. processor_model / release_year are the
family's CPU and GA year (curated). storage_* is populated only for DenseIO / GPU shapes
with local NVMe.
"""

import argparse
import json
import os
import sys
import time
import urllib.request

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))

PRODUCTS_URL = ("https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/"
                "?currencyCode=USD")
WORK = os.path.join(HERE, "work", "oracle")

# A flexible shape lets you pick memory freely (OCI allows 1-64 GB/OCPU) and prices it
# strictly linearly -- ocpu_rate*OCPUs + mem_rate*GB, with NO premium for a custom ratio
# (unlike GCP, where non-default configs cost ~10% more). A single ratio would collapse the
# RAM axis and make the clouds hard to compare, so each flexible shape is enumerated at four
# memory points -- these multiples of the family's default GB/OCPU ratio (16 GB/OCPU for x86
# / AmpereOne, 6 GB/OCPU for Altra A1). On x86 this spans 2 / 4 / 8 / 16 GB per vCPU, i.e. a
# "compute" point matching AWS c-series (2 GB/vCPU), a "general" point, OCI's console default
# (1.0), and a "memory" point. Each stays inside OCI's 1-64 GB/OCPU bounds; points that
# collide at a family's memory cap are de-duplicated.
MEM_TIERS = (0.25, 0.5, 1.0, 2.0)

# OCPU counts we enumerate flexible shapes at (truncated to each family's max, which is
# always appended so the top of the range is represented).
FLEX_SIZES = [1, 2, 4, 8, 16, 32, 64]


# --------------------------------------------------------------------------- #
# CATALOG -- curated shape specs. Prices come from the live catalog by part number.
# --------------------------------------------------------------------------- #
# Each flexible family: the catalog part numbers for its OCPU-hour and memory-GB-hour SKUs,
# the CPU, arch, vCPU/core multipliers per OCPU, the max OCPU, the network cap (Gbit/s) and
# per-OCPU network rate, the default memory ratio, the max memory (GiB), the category, the
# release year, and whether it is a current generation.
#
# Specs verified against docs.oracle.com/.../Compute/References/computeshapes.htm (OCPU
# ranges, memory caps, network, CPU SKU, DenseIO configs, GPU counts/memory all "High"
# confidence); CPU generation code-names and GA years from Oracle/AMD/Ampere/NVIDIA
# announcements. E2/Standard2/E3 are previous-generation (is_current = false).
#
# mem_per_ocpu is the family's default GB/OCPU ratio; each shape is enumerated at 0.25x /
# 0.5x / 1x / 2x of it (see MEM_TIERS). Fields: (shape, cpu, arch, vcpu_per_ocpu, cores_per_ocpu,
#   max_ocpu, net_per_ocpu, net_cap, mem_per_ocpu, mem_max, category, year, is_current,
#   ocpu_part, mem_part)
FLEX = [
    ("VM.Standard3.Flex", "Intel Xeon Platinum 8358 (Ice Lake)", "x86_64", 2, 1,
     32, 1.0, 32.0, 16.0, 512.0, "General purpose", 2022.0, True, "B94176", "B94177"),
    ("VM.Optimized3.Flex", "Intel Xeon 6354 (Ice Lake)", "x86_64", 2, 1,
     18, 4.0, 40.0, 16.0, 256.0, "Compute optimized", 2021.0, True, "B93311", "B93312"),
    ("VM.Standard.E3.Flex", "AMD EPYC 7742 (Rome)", "x86_64", 2, 1,
     64, 1.0, 40.0, 16.0, 1024.0, "General purpose", 2020.0, False, "B92306", "B92307"),
    ("VM.Standard.E4.Flex", "AMD EPYC 7J13 (Milan)", "x86_64", 2, 1,
     64, 1.0, 40.0, 16.0, 1024.0, "General purpose", 2021.0, True, "B93113", "B93114"),
    ("VM.Standard.E5.Flex", "AMD EPYC 9J14 (Genoa)", "x86_64", 2, 1,
     126, 1.0, 40.0, 16.0, 1049.0, "General purpose", 2023.0, True, "B97384", "B97385"),
    ("VM.Standard.E6.Flex", "AMD EPYC 9J45 (Turin)", "x86_64", 2, 1,
     126, 1.0, 99.0, 16.0, 1454.0, "General purpose", 2025.0, True, "B111129", "B111130"),
    ("VM.Standard.A1.Flex", "Ampere Altra Q80-30", "arm64", 1, 1,
     76, 1.0, 40.0, 6.0, 472.0, "General purpose", 2021.0, True, "B93297", "B93298"),
    ("VM.Standard.A2.Flex", "Ampere AmpereOne A160-30", "arm64", 2, 2,
     78, 1.0, 78.0, 16.0, 946.0, "General purpose", 2024.0, True, "B109529", "B109530"),
    ("VM.Standard.A4.Flex", "Ampere AmpereOne M A06-36M", "arm64", 2, 2,
     45, 1.0, 100.0, 16.0, 700.0, "General purpose", 2025.0, True, "B112145", "B112146"),
]

# Fixed previous-generation VM shapes: fixed vCPU/RAM, priced per OCPU (memory bundled into
# the OCPU rate -- these families have no separate memory SKU). All is_current = false.
# Fields: (instance, family, cpu, arch, vcpus, cores, ram_gib, net_gbps, category, year,
#          is_current, ocpu_part, ocpus)
FIXED = [
    ("VM.Standard.E2.1", "VM.Standard.E2", "AMD EPYC 7551 (Naples)", "x86_64",
     2, 1, 8.0, 0.7, "General purpose", 2018.0, False, "B90425", 1),
    ("VM.Standard.E2.2", "VM.Standard.E2", "AMD EPYC 7551 (Naples)", "x86_64",
     4, 2, 16.0, 1.4, "General purpose", 2018.0, False, "B90425", 2),
    ("VM.Standard.E2.4", "VM.Standard.E2", "AMD EPYC 7551 (Naples)", "x86_64",
     8, 4, 32.0, 2.8, "General purpose", 2018.0, False, "B90425", 4),
    ("VM.Standard.E2.8", "VM.Standard.E2", "AMD EPYC 7551 (Naples)", "x86_64",
     16, 8, 64.0, 5.6, "General purpose", 2018.0, False, "B90425", 8),
    # Always Free micro shape -- priced at $0 (free-tier SKU); isolated in oracle_shared.
    ("VM.Standard.E2.1.Micro", "VM.Standard.E2.Micro", "AMD EPYC 7551 (Naples)", "x86_64",
     2, 1, 1.0, 0.48, "General purpose", 2019.0, False, "B91444", 1),
    ("VM.Standard2.1", "VM.Standard2", "Intel Xeon Platinum 8167M (Skylake)", "x86_64",
     2, 1, 15.0, 1.0, "General purpose", 2018.0, False, "B88317", 1),
    ("VM.Standard2.2", "VM.Standard2", "Intel Xeon Platinum 8167M (Skylake)", "x86_64",
     4, 2, 30.0, 2.0, "General purpose", 2018.0, False, "B88317", 2),
    ("VM.Standard2.4", "VM.Standard2", "Intel Xeon Platinum 8167M (Skylake)", "x86_64",
     8, 4, 60.0, 4.1, "General purpose", 2018.0, False, "B88317", 4),
    ("VM.Standard2.8", "VM.Standard2", "Intel Xeon Platinum 8167M (Skylake)", "x86_64",
     16, 8, 120.0, 8.2, "General purpose", 2018.0, False, "B88317", 8),
    ("VM.Standard2.16", "VM.Standard2", "Intel Xeon Platinum 8167M (Skylake)", "x86_64",
     32, 16, 240.0, 16.4, "General purpose", 2018.0, False, "B88317", 16),
    ("VM.Standard2.24", "VM.Standard2", "Intel Xeon Platinum 8167M (Skylake)", "x86_64",
     48, 24, 320.0, 24.6, "General purpose", 2018.0, False, "B88317", 24),
]

# Bare-metal shapes (whole-server; excluded from the comparable `oracle` view). Priced per
# OCPU + memory-GB using the family's VM SKUs. Fields: (instance, family, cpu, arch, ocpus,
# vcpus, cores, ram_gib, net_gbps, storage_gb, storage_count, year, is_current, ocpu_part,
# mem_part). category is always "Bare metal".
METAL = [
    ("BM.Standard.E4.128", "BM.Standard.E4", "AMD EPYC 7J13 (Milan)", "x86_64",
     128, 256, 128, 2048.0, 100.0, None, None, 2021.0, True, "B93113", "B93114"),
    ("BM.Standard.E5.192", "BM.Standard.E5", "AMD EPYC 9J14 (Genoa)", "x86_64",
     192, 384, 192, 2304.0, 100.0, None, None, 2023.0, True, "B97384", "B97385"),
    ("BM.Standard.E6.256", "BM.Standard.E6", "AMD EPYC 9J45 (Turin)", "x86_64",
     256, 512, 256, 3072.0, 200.0, None, None, 2025.0, True, "B111129", "B111130"),
    ("BM.Standard3.64", "BM.Standard3", "Intel Xeon Platinum 8358 (Ice Lake)", "x86_64",
     64, 128, 64, 1024.0, 100.0, None, None, 2022.0, True, "B94176", "B94177"),
    ("BM.Optimized3.36", "BM.Optimized3", "Intel Xeon 6354 (Ice Lake)", "x86_64",
     36, 72, 36, 512.0, 100.0, 3840, 1, 2021.0, True, "B93311", "B93312"),
    ("BM.Standard.A1.160", "BM.Standard.A1", "Ampere Altra Q80-30", "arm64",
     160, 160, 160, 1024.0, 100.0, None, None, 2021.0, True, "B93297", "B93298"),
]

# DenseIO shapes (local NVMe). One row per documented (ocpu, mem, nvme) config; VM.DenseIO
# flex shapes are enumerated as <shape>.<ocpus>, the bare-metal ones carry their fixed
# OCPU count as the suffix. Priced per OCPU + memory + NVMe-TB. storage_read/write_iops are
# Oracle's single minimum-guaranteed-IOPS SLA floor (used for both). Fields: (shape, cpu,
#   arch, vcpu_per_ocpu, cores_per_ocpu, category, year, is_current, ocpu_part, mem_part,
#   nvme_part, configs=[(ocpu, ram_gib, nvme_gb, ndrives, net_gbps, read_iops, write_iops)])
DENSEIO = [
    ("VM.DenseIO.E4.Flex", "AMD EPYC 7J13 (Milan)", "x86_64", 2, 1,
     "Storage optimized", 2022.0, True, "B93121", "B93122", "B93123", [
         (8, 128.0, 6800, 1, 8.0, 230000, 230000),
         (16, 256.0, 13600, 2, 16.0, 460000, 460000),
         (32, 512.0, 27200, 4, 32.0, 920000, 920000),
     ]),
    ("VM.DenseIO.E5.Flex", "AMD EPYC 9J14 (Genoa)", "x86_64", 2, 1,
     "Storage optimized", 2024.0, True, "B98202", "B98203", "B98204", [
         (8, 96.0, 6800, 1, 8.0, 290000, 290000),
         (16, 192.0, 13600, 2, 16.0, 580000, 580000),
         (24, 288.0, 20400, 3, 24.0, 870000, 870000),
         (32, 384.0, 27200, 4, 32.0, 1160000, 1160000),
         (40, 480.0, 34000, 5, 40.0, 1450000, 1450000),
         (48, 576.0, 40800, 6, 48.0, 1740000, 1740000),
     ]),
    ("BM.DenseIO.E4", "AMD EPYC 7J13 (Milan)", "x86_64", 2, 1,
     "Bare metal", 2022.0, True, "B93121", "B93122", "B93123", [
         (128, 2048.0, 54400, 8, 100.0, 1880000, 1880000),
     ]),
    ("BM.DenseIO.E5", "AMD EPYC 9J14 (Genoa)", "x86_64", 2, 1,
     "Bare metal", 2024.0, True, "B98202", "B98203", "B98204", [
         (128, 1536.0, 81600, 12, 100.0, 3400000, 3400000),
     ]),
]

# GPU shapes. Priced as gpu_rate * nGPU (host CPU/RAM/local NVMe bundled). vcpus/cores are
# the host CPU (x86 hosts: vcpus = 2*ocpu, cores = ocpu; the GB200 host is Grace Arm).
# Fields: (instance, family, host_cpu, arch, vcpus, cores, ram_gib, gpu_model, n_gpu,
#   gpu_gib, net_gbps, storage_gb, storage_count, year, is_current, gpu_part)
GPU = [
    ("VM.GPU2.1", "VM.GPU2", "Intel Xeon Platinum 8167M", "x86_64", 24, 12, 72.0,
     "NVIDIA Tesla P100", 1, 16.0, 8.0, None, None, 2018.0, False, "B88518"),
    ("VM.GPU3.1", "VM.GPU3", "Intel Xeon Platinum 8167M", "x86_64", 12, 6, 90.0,
     "NVIDIA Tesla V100", 1, 16.0, 4.0, None, None, 2018.0, False, "B89734"),
    ("VM.GPU3.2", "VM.GPU3", "Intel Xeon Platinum 8167M", "x86_64", 24, 12, 180.0,
     "NVIDIA Tesla V100", 2, 16.0, 8.0, None, None, 2018.0, False, "B89734"),
    ("VM.GPU3.4", "VM.GPU3", "Intel Xeon Platinum 8167M", "x86_64", 48, 24, 360.0,
     "NVIDIA Tesla V100", 4, 16.0, 24.6, None, None, 2018.0, False, "B89734"),
    ("VM.GPU.A10.1", "VM.GPU.A10", "Intel Xeon Platinum 8358", "x86_64", 30, 15, 240.0,
     "NVIDIA A10", 1, 24.0, 24.0, None, None, 2022.0, True, "B95909"),
    ("VM.GPU.A10.2", "VM.GPU.A10", "Intel Xeon Platinum 8358", "x86_64", 60, 30, 480.0,
     "NVIDIA A10", 2, 24.0, 48.0, None, None, 2022.0, True, "B95909"),
    ("BM.GPU.A10.4", "BM.GPU.A10", "Intel Xeon Platinum 8358", "x86_64", 128, 64, 1024.0,
     "NVIDIA A10", 4, 24.0, 100.0, 7680, 2, 2022.0, True, "B95909"),
    ("BM.GPU4.8", "BM.GPU4", "AMD EPYC 7542 (Rome)", "x86_64", 128, 64, 2048.0,
     "NVIDIA A100 40GB", 8, 40.0, 50.0, 27200, 4, 2021.0, False, "B92740"),
    ("BM.GPU.A100-v2.8", "BM.GPU.A100-v2", "AMD EPYC 7J13 (Milan)", "x86_64", 256, 128,
     2048.0, "NVIDIA A100 80GB", 8, 80.0, 100.0, 27200, 4, 2022.0, True, "B95907"),
    ("BM.GPU.L40S.4", "BM.GPU.L40S", "Intel Xeon 8480+ (Sapphire Rapids)", "x86_64",
     224, 112, 1024.0, "NVIDIA L40S", 4, 48.0, 200.0, 7680, 2, 2024.0, True, "B109479"),
    ("BM.GPU.H100.8", "BM.GPU.H100", "Intel Xeon 8480+ (Sapphire Rapids)", "x86_64",
     224, 112, 2048.0, "NVIDIA H100 80GB", 8, 80.0, 100.0, 61440, 16, 2023.0, True,
     "B98415"),
    ("BM.GPU.H200.8", "BM.GPU.H200", "Intel Xeon 8480+ (Sapphire Rapids)", "x86_64",
     224, 112, 3072.0, "NVIDIA H200 141GB", 8, 141.0, 200.0, 30720, 8, 2024.0, True,
     "B110519"),
    ("BM.GPU.MI300X.8", "BM.GPU.MI300X", "Intel Xeon 8480+ (Sapphire Rapids)", "x86_64",
     224, 112, 2048.0, "AMD Instinct MI300X", 8, 192.0, 100.0, 30720, 8, 2024.0, True,
     "B109485"),
    ("BM.GPU.B200.8", "BM.GPU.B200", "Intel Xeon 8592+ (Emerald Rapids)", "x86_64",
     256, 128, 4096.0, "NVIDIA B200 180GB", 8, 180.0, 400.0, 30720, 8, 2025.0, True,
     "B110978"),
    ("BM.GPU.GB200.4", "BM.GPU.GB200", "NVIDIA Grace (Arm)", "arm64", 144, 144, 960.0,
     "NVIDIA GB200 192GB", 4, 192.0, 400.0, 30720, 4, 2025.0, True, "B110979"),
]


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
# Pricing
# --------------------------------------------------------------------------- #
def fetch_prices(refresh):
    """part number -> PAY_AS_YOU_GO USD rate (the marginal / non-free tier rate)."""
    print("Fetching OCI cost-estimator product catalog ...")
    data = json.loads(_cached("products-USD.json", PRODUCTS_URL, refresh))
    items = data.get("items") or []
    prices = {}
    for i in items:
        pn = i.get("partNumber")
        if not pn:
            continue
        for loc in i.get("currencyCodeLocalizations") or []:
            if loc.get("currencyCode") != "USD":
                continue
            vals = [p.get("value") for p in loc.get("prices") or []
                    if p.get("model") == "PAY_AS_YOU_GO" and p.get("value") is not None]
            if vals:
                prices[pn] = vals[-1]      # last range = marginal rate (past any free tier)
    print(f"  {len(items)} products, {len(prices)} PAYG USD rates")
    return prices


def _rate(prices, part, what):
    r = prices.get(part)
    if r is None:
        raise SystemExit(f"catalog is missing the {what} rate (part {part}); "
                         f"re-run with --refresh or update the CATALOG part numbers")
    return r


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

# family names sent to the "burst" (free-tier micro) tier and excluded from the `oracle`
# comparable view.
MICRO_FAMILIES = ["VM.Standard.E2.Micro"]


def _row(instance, family, category, price_hour, ram_gib, vcpus, cores, cpu, arch,
         net, storage_gb=None, storage_count=None, is_ssd=None, is_nvme=None,
         accelerators=0, acc_model=None, acc_gib=None, is_current=True,
         read_iops=None, write_iops=None, year=None):
    """One 27-column row; vcpus_base == vcpus (OCI has no sub-baseline shapes),
    net_peak == net (one bandwidth level), ebs_* always null."""
    return (
        instance, family, category, price_hour, ram_gib, vcpus, float(vcpus), cores,
        cpu, arch, net, net, storage_gb, storage_count, is_ssd, is_nvme,
        None, None, None, None, accelerators, acc_model, acc_gib, is_current,
        read_iops, write_iops, year,
    )


def _flex_sizes(max_ocpu):
    return [n for n in FLEX_SIZES if n < max_ocpu] + [max_ocpu]


def build_rows(prices):
    """Every shape as a 27-column row. Returns (rows, burst_families)."""
    rows = []

    for (shape, cpu, arch, v_per, c_per, max_ocpu, net_per, net_cap,
         mem_per, mem_max, category, year, current, ocpu_part, mem_part) in FLEX:
        orate = _rate(prices, ocpu_part, f"{shape} OCPU")
        mrate = _rate(prices, mem_part, f"{shape} memory")
        for ocpu in _flex_sizes(max_ocpu):
            net = min(ocpu * net_per, net_cap)
            seen = set()
            for mult in MEM_TIERS:
                ram = int(min(round(ocpu * mem_per * mult), mem_max))
                if ram in seen:                # tiers collapse at the memory cap
                    continue
                seen.add(ram)
                rows.append(_row(
                    f"{shape}.{ocpu}-{ram}", shape, category,
                    round(ocpu * orate + ram * mrate, 6), float(ram),
                    ocpu * v_per, ocpu * c_per, cpu, arch, float(net),
                    is_current=current, year=year))

    for (instance, family, cpu, arch, vcpus, cores, ram, net, category, year,
         current, ocpu_part, ocpus) in FIXED:
        orate = _rate(prices, ocpu_part, f"{instance} OCPU")
        rows.append(_row(
            instance, family, category, round(ocpus * orate, 6), float(ram),
            vcpus, cores, cpu, arch, float(net) if net else None,
            is_current=current, year=year))

    for (instance, family, cpu, arch, ocpus, vcpus, cores, ram, net, s_gb, s_cnt,
         year, current, ocpu_part, mem_part) in METAL:
        price = ocpus * _rate(prices, ocpu_part, f"{instance} OCPU")
        if mem_part:
            price += ram * _rate(prices, mem_part, f"{instance} memory")
        is_ssd = True if s_gb else None
        rows.append(_row(
            instance, family, "Bare metal", round(price, 6), float(ram),
            vcpus=vcpus, cores=cores, cpu=cpu, arch=arch,
            net=float(net) if net else None, storage_gb=s_gb, storage_count=s_cnt,
            is_ssd=is_ssd, is_nvme=is_ssd, is_current=current, year=year))

    for (shape, cpu, arch, v_per, c_per, category, year, current,
         ocpu_part, mem_part, nvme_part, configs) in DENSEIO:
        orate = _rate(prices, ocpu_part, f"{shape} OCPU")
        mrate = _rate(prices, mem_part, f"{shape} memory")
        nrate = _rate(prices, nvme_part, f"{shape} NVMe")
        for (ocpu, ram, nvme_gb, ndrives, net, r_iops, w_iops) in configs:
            price = ocpu * orate + ram * mrate + (nvme_gb / 1000.0) * nrate
            rows.append(_row(
                f"{shape}.{ocpu}", shape, category, round(price, 6), float(ram),
                ocpu * v_per, ocpu * c_per, cpu, arch, float(net),
                storage_gb=nvme_gb, storage_count=ndrives, is_ssd=True, is_nvme=True,
                is_current=current, read_iops=r_iops, write_iops=w_iops, year=year))

    for (instance, family, cpu, arch, vcpus, cores, ram, gpu_model, n_gpu, gpu_gib,
         net, s_gb, s_cnt, year, current, gpu_part) in GPU:
        grate = _rate(prices, gpu_part, f"{instance} GPU")
        is_ssd = True if s_gb else None
        rows.append(_row(
            instance, family, "GPU", round(n_gpu * grate, 6), float(ram),
            vcpus, cores, cpu, arch, float(net) if net else None,
            storage_gb=s_gb, storage_count=s_cnt, is_ssd=is_ssd, is_nvme=is_ssd,
            accelerators=n_gpu, acc_model=gpu_model,
            acc_gib=float(n_gpu * gpu_gib) if gpu_gib else None,
            is_current=current, year=year))

    n_gpu = sum(1 for r in rows if r[2] == "GPU")
    print(f"  {len(rows)} shapes ({n_gpu} GPU)")
    return rows, list(MICRO_FAMILIES)


# --------------------------------------------------------------------------- #
# Views + column comments
# --------------------------------------------------------------------------- #
def views_sql(burst_families):
    burst = ", ".join(f"'{f}'" for f in burst_families) or "''"
    return f"""
create view oracle as
  select instance, family, category, price_hour, release_year, ram_gib, vcpus, cores, processor_model, arch, net_gbitps, net_peak_gbitps, storage_gb, storage_count, storage_read_iops, storage_write_iops, storage_is_ssd, storage_is_nvme, ebs_iops, ebs_gbitps, ebs_peak_iops, ebs_peak_gbitps
  from oracle_all
  where is_current
  and price_hour is not null
  and accelerators = 0
  and category != 'Bare metal'
  and family not in ({burst});
create view oracle_family as
  select * from oracle join
  (select case when two.instance is null then one.instance
              when ((two.net_gbitps/two.price_hour)/(one.net_gbitps/one.price_hour) > 1.1) then two.instance
              else one.instance end as instance
  from (select * from (select *, row_number() over (partition by family order by vcpus desc) r from oracle) where r = 1) one
  left join (select * from (select *, row_number() over (partition by family order by vcpus desc) r from oracle) where r = 2) two
  using (family)) using (instance);
create view oracle_accel as
  select * from oracle_all
  where category = 'GPU' and accelerator_model is not null;
create view oracle_shared as
  select * from oracle_all where family in ({burst});
COMMENT ON COLUMN oracle_all.instance IS 'OCI shape name; flexible shapes carry a ".<OCPUs>-<GB>" size suffix (e.g. VM.Standard.E5.Flex.8-128), enumerated at three memory ratios per OCPU count';
COMMENT ON COLUMN oracle_all.price_hour IS 'Reference-region Linux hourly on-demand price in USD, assembled from the family OCPU-hour + memory-GB-hour (+ GPU-hour) catalog rates';
COMMENT ON COLUMN oracle_all.family IS 'Shape family: the shape name (flexible) or its prefix before the size (e.g. VM.Standard.E5.Flex, VM.Standard2)';
COMMENT ON COLUMN oracle_all.category IS 'Shape category (General purpose, Compute optimized, Storage optimized, Bare metal, GPU)';
COMMENT ON COLUMN oracle_all.ram_gib IS 'Amount of main memory in GiB';
COMMENT ON COLUMN oracle_all.vcpus IS 'Number of vCPUs: 2 per OCPU on x86 and AmpereOne (A2/A4), 1 per OCPU on Ampere Altra (A1)';
COMMENT ON COLUMN oracle_all.vcpus_base IS 'Same as vcpus (OCI has no burstable/sub-baseline shapes)';
COMMENT ON COLUMN oracle_all.cores IS 'Number of physical cores: 1 per OCPU on x86 and Altra (A1), 2 per OCPU on AmpereOne (A2/A4)';
COMMENT ON COLUMN oracle_all.processor_model IS 'CPU model of the shape family (curated)';
COMMENT ON COLUMN oracle_all.arch IS 'Processor architecture (x86_64 or arm64)';
COMMENT ON COLUMN oracle_all.net_gbitps IS 'Maximum network bandwidth in Gbit/s (scales with OCPU up to a family cap)';
COMMENT ON COLUMN oracle_all.net_peak_gbitps IS 'Same as net_gbitps (OCI publishes one bandwidth level)';
COMMENT ON COLUMN oracle_all.storage_gb IS 'Total local NVMe in GB (DenseIO / GPU shapes; NULL when the shape has no local disk)';
COMMENT ON COLUMN oracle_all.storage_count IS 'Number of local NVMe drives';
COMMENT ON COLUMN oracle_all.storage_is_ssd IS 'Whether local storage is SSD/NVMe';
COMMENT ON COLUMN oracle_all.storage_is_nvme IS 'Whether local storage is directly-attached NVMe';
COMMENT ON COLUMN oracle_all.ebs_iops IS 'Always NULL -- OCI block-volume performance is per-volume, not per-shape';
COMMENT ON COLUMN oracle_all.ebs_gbitps IS 'Always NULL (see ebs_iops)';
COMMENT ON COLUMN oracle_all.ebs_peak_iops IS 'Always NULL (see ebs_iops)';
COMMENT ON COLUMN oracle_all.ebs_peak_gbitps IS 'Always NULL (see ebs_iops)';
COMMENT ON COLUMN oracle_all.accelerators IS 'Number of attached GPUs';
COMMENT ON COLUMN oracle_all.accelerator_model IS 'GPU model (curated)';
COMMENT ON COLUMN oracle_all.accelerator_gib IS 'Total GPU memory in GiB';
COMMENT ON COLUMN oracle_all.is_current IS 'Whether the shape is a current generation (legacy families marked false)';
COMMENT ON COLUMN oracle_all.storage_read_iops IS 'Local NVMe random read IOPS (DenseIO; curated)';
COMMENT ON COLUMN oracle_all.storage_write_iops IS 'Local NVMe random write IOPS (DenseIO; curated)';
COMMENT ON COLUMN oracle_all.release_year IS 'Approximate family GA year (curated; OCI publishes no per-shape GA date)';
"""


def write_duckdb(rows, burst_families, out_path):
    con = duckdb.connect(out_path)
    cols_ddl = ", ".join(f'"{n}" {t}' for n, t in COLUMNS)
    for v in ("oracle_family", "oracle_accel", "oracle_shared", "oracle"):
        con.execute(f"drop view if exists {v}")
    con.execute("drop table if exists oracle_all cascade")
    con.execute(f"create table oracle_all ({cols_ddl})")
    con.executemany(
        f"insert into oracle_all values ({', '.join(['?'] * len(COLUMNS))})", rows
    )
    con.execute(views_sql(burst_families))
    counts = {
        v: con.execute(f"select count(*) from {v}").fetchone()[0]
        for v in ("oracle_all", "oracle", "oracle_family", "oracle_accel", "oracle_shared")
    }
    con.close()
    print("Wrote " + out_path)
    for v, c in counts.items():
        print(f"  {v}: {c}")


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--output", default=os.path.join(HERE, "cloudspecs.duckdb"),
                    help="DuckDB file to write the oracle_all table + views into")
    ap.add_argument("--refresh", action="store_true",
                    help="re-download the cached product/price catalog")
    args = ap.parse_args()

    prices = fetch_prices(args.refresh)
    print("Building rows ...")
    rows, burst = build_rows(prices)
    write_duckdb(rows, burst, args.output)


if __name__ == "__main__":
    sys.exit(main())
