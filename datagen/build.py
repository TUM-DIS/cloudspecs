#!/usr/bin/env python3
"""
Build cloudspecs.duckdb for AWS EC2 from scratch.

Self-contained rewrite of the old ec2instances.info scraper + cloudspecs/toduckdb.sh.
Only Linux on-demand prices in us-east-1 are collected.

Data sources (all robust, minimal dependencies):
  1. EC2 DescribeInstanceTypes (boto3, describe-style API) -> all hardware specs
  2. Public AWS bulk price list JSON for us-east-1 (no credentials) -> price,
     category (instanceFamily) and processor model (physicalProcessor)
  3. instancetyp.es/timeline.json (external) -> release dates
  4. AWS docs HTML (instance store spec pages) -> instance-store read/write IOPS
  5. benchmark.csv (static, shipped with this directory) -> SPEC benchmarks
  6. A small static table for burstable (T2/T3/T3a/T4g) baseline performance

Output: cloudspecs.duckdb, containing table `aws_all` (+ `benchmark`) and the
views `aws`, `aws_family`, `aws_accel`, `aws_burst`.
"""

import argparse
import json
import os
import sys
import urllib.request

import boto3
import duckdb
from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "work")

REGION = "us-east-1"

# Public AWS Price List Bulk API, us-east-1 offer file. No credentials required.
PRICE_URL = (
    "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/"
    "AmazonEC2/current/us-east-1/index.json"
)
PRICE_CACHE = os.path.join(WORK, "useast1_index.json")

# External release-date timeline (same data the old toduckdb.sh used).
TIMELINE_URL = "https://instancetyp.es/timeline.json"
TIMELINE_CACHE = os.path.join(WORK, "timeline.json")

# AWS docs "instance store specifications" pages (read/write IOPS for NVMe storage).
STORAGE_DOC_BASE = "https://docs.aws.amazon.com/ec2/latest/instancetypes/"
STORAGE_DOC_PAGES = ["gp", "co", "mo", "so", "ac", "hpc", "pg"]

MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11,
    "December": 12,
}

# Burstable baseline: CPU credits earned per hour (AWS published values).
# base_performance == credits/60, and that is used as `vcpus_base`.
# T3a and T4g share the T3 numbers.
_T2_CREDITS = {"nano": 3, "micro": 6, "small": 12, "medium": 24, "large": 36,
               "xlarge": 54, "2xlarge": 81.6}
_T3_CREDITS = {"nano": 6, "micro": 12, "small": 24, "medium": 24, "large": 36,
               "xlarge": 96, "2xlarge": 192}
BURST_CREDITS = {}
for _size, _c in _T2_CREDITS.items():
    BURST_CREDITS[("t2", _size)] = _c
for _fam in ("t3", "t3a", "t4g"):
    for _size, _c in _T3_CREDITS.items():
        BURST_CREDITS[(_fam, _size)] = _c


# --------------------------------------------------------------------------- #
# Download helpers
# --------------------------------------------------------------------------- #
def _download(url, path):
    """Download url to path unless it already exists (cache)."""
    if os.path.exists(path):
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    print(f"  downloading {url}")
    tmp = path + ".tmp"
    with urllib.request.urlopen(url) as resp, open(tmp, "wb") as f:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    os.replace(tmp, path)
    return path


# --------------------------------------------------------------------------- #
# 1. DescribeInstanceTypes -> hardware specs
# --------------------------------------------------------------------------- #
def describe_instance_types(region=REGION):
    """Return {instance_type: api_description} from DescribeInstanceTypes."""
    print(f"DescribeInstanceTypes ({region}) ...")
    client = boto3.client("ec2", region_name=region)
    result = {}
    for page in client.get_paginator("describe_instance_types").paginate():
        for it in page["InstanceTypes"]:
            result[it["InstanceType"]] = it
    print(f"  {len(result)} instance types described")
    return result


# --------------------------------------------------------------------------- #
# 2. Bulk price list -> price, category, processor model, current-generation
# --------------------------------------------------------------------------- #
def load_pricing():
    """Return {instance_type: {price, category, processor, current, vcpu, memory}}.

    Only Linux / Shared tenancy / No License / no pre-installed software /
    used capacity on-demand offers are considered.
    """
    print("Loading us-east-1 bulk price list ...")
    _download(PRICE_URL, PRICE_CACHE)
    with open(PRICE_CACHE) as f:
        data = json.load(f)

    products = data["products"]
    ondemand = data["terms"]["OnDemand"]
    out = {}
    for sku, product in products.items():
        if product.get("productFamily") not in (
            "Compute Instance",
            "Compute Instance (bare metal)",
        ):
            continue
        a = product["attributes"]
        itype = a.get("instanceType")
        if not itype:
            continue
        if not (
            a.get("operatingSystem") == "Linux"
            and a.get("tenancy") == "Shared"
            and a.get("licenseModel") == "No License required"
            and a.get("preInstalledSw") == "NA"
            and a.get("capacitystatus") == "Used"
            # skip CapacityBlock SKUs (priced at $0) so we keep the real on-demand rate
            and a.get("marketoption") == "OnDemand"
        ):
            continue
        terms = ondemand.get(sku)
        if not terms:
            continue
        price = None
        for term in terms.values():
            for dim in term["priceDimensions"].values():
                usd = dim.get("pricePerUnit", {}).get("USD")
                if usd is not None:
                    price = float(usd)
        if price is None:
            continue
        out[itype] = {
            "price": price,
            "category": a.get("instanceFamily"),
            "processor": a.get("physicalProcessor"),
            "current": a.get("currentGeneration") == "Yes",
            "vcpu": a.get("vcpu"),
            "memory": a.get("memory"),
        }
    print(f"  {len(out)} instance types with a us-east-1 Linux on-demand price")
    return out


# --------------------------------------------------------------------------- #
# 3. Release dates
# --------------------------------------------------------------------------- #
def load_release_years():
    """Return {instance_type: release_year_fraction} rounded like the old build."""
    print("Loading release timeline ...")
    try:
        _download(TIMELINE_URL, TIMELINE_CACHE)
        with open(TIMELINE_CACHE) as f:
            data = json.load(f)
    except Exception as e:  # external service; do not hard-fail the build
        print(f"  WARNING: could not load timeline ({e}); release_year will be null")
        return {}
    out = {}
    for row in data.get("instances", []):
        itype = row.get("instance_type")
        month = MONTHS.get(row.get("release_month"))
        year = row.get("release_year")
        if itype and month and year:
            out[itype] = round(year + (month - 1) / 12.0, 2)
    print(f"  {len(out)} release dates")
    return out


# --------------------------------------------------------------------------- #
# 4. Instance-store IOPS from AWS docs
# --------------------------------------------------------------------------- #
def _parse_storage_iops_page(html):
    """Yield (instance_type, read_iops, write_iops) from one AWS doc page."""
    soup = BeautifulSoup(html, "html.parser")
    header = soup.find("h2", string="Instance store specifications")
    if header is None:
        return
    table = header.find_next("table")
    if table is None:
        return
    for row in table.find_all("tr")[1:]:
        cols = row.find_all("td")
        if len(cols) < 4:
            continue
        name_cell = cols[0]
        for sup in name_cell.find_all("sup"):
            sup.decompose()
        itype = name_cell.get_text(strip=True)
        text = cols[3].get_text(strip=True)
        if not text:
            yield itype, None, None
            continue
        parts = text.split("/")
        try:
            read = int("".join(c for c in parts[0] if c.isdigit()))
            write = int("".join(c for c in parts[-1] if c.isdigit()))
        except ValueError:
            continue
        yield itype, read, write


def load_storage_iops():
    """Return {instance_type: (read_iops, write_iops)} scraped from AWS docs."""
    print("Scraping instance-store IOPS from AWS docs ...")
    out = {}
    for page in STORAGE_DOC_PAGES:
        path = os.path.join(WORK, f"{page}.html")
        try:
            _download(STORAGE_DOC_BASE + f"{page}.html", path)
            with open(path, encoding="utf-8") as f:
                html = f.read()
            for itype, read, write in _parse_storage_iops_page(html):
                out[itype] = (read, write)
        except Exception as e:
            print(f"  WARNING: storage page {page}.html failed ({e})")
    print(f"  {len(out)} instance-store IOPS rows")
    return out


# --------------------------------------------------------------------------- #
# Per-field derivations (ported from the old chair filters / toduckdb.sh jq)
# --------------------------------------------------------------------------- #
def _family(itype):
    return itype.split(".")[0]


def _is_flex(itype):
    parts = _family(itype).split("-")
    return len(parts) > 1 and parts[1] == "flex"


def vcpus_base(itype, vcpus):
    """Baseline vCPUs: flex -> 0.4*vCPU, burstable -> credits/60, else vCPU."""
    if _is_flex(itype):
        return 0.4 * vcpus
    fam = _family(itype)
    size = itype.split(".", 1)[1] if "." in itype else ""
    credits = BURST_CREDITS.get((fam, size))
    if credits is not None:
        return credits / 60.0
    return float(vcpus)


def network_bandwidth(api):
    """Return (baseline_gbps, peak_gbps) summed over network cards, capped by
    the advertised NetworkPerformance. Mirrors the old chair_add_network_info."""
    if api is None:
        return None, None
    netinfo = api["NetworkInfo"]
    try:
        max_peak = int(netinfo["NetworkPerformance"].split(" ")[0])
    except ValueError:
        max_peak = None
    baseline = None
    peak = None
    for card in netinfo.get("NetworkCards", []):
        if "BaselineBandwidthInGbps" in card:
            baseline = (baseline or 0) + card["BaselineBandwidthInGbps"]
            if "PeakBandwidthInGbps" in card:
                peak = (peak or 0) + card["PeakBandwidthInGbps"]
        else:
            try:
                gbps = int(card["NetworkPerformance"].split(" ")[0])
            except (KeyError, ValueError):
                continue
            baseline = (baseline or 0) + gbps
            peak = (peak or 0) + gbps
    if max_peak is not None:
        if baseline is not None:
            baseline = min(baseline, max_peak)
        if peak is not None:
            peak = min(peak, max_peak)
    return baseline, peak


# DescribeInstanceTypes accelerator blocks, in priority order:
# (info key, device-list key, total-memory key, prefix model with manufacturer).
# Neuron is the only one that reports no manufacturer.
_ACCELERATORS = [
    ("GpuInfo", "Gpus", "TotalGpuMemoryInMiB", True),
    ("NeuronInfo", "NeuronDevices", "TotalNeuronDeviceMemoryInMiB", False),
    ("FpgaInfo", "Fpgas", "TotalFpgaMemoryInMiB", True),
    ("MediaAcceleratorInfo", "Accelerators", "TotalMediaMemoryInMiB", True),
]


def accelerator_info(api):
    """Return (count, model, gib) for GPU/FPGA/Neuron/Media accelerators."""
    if api is None:
        return 0, None, None
    for info_key, list_key, mem_key, with_mfr in _ACCELERATORS:
        if info_key not in api:
            continue
        info = api[info_key]
        dev = info[list_key][0]
        model = f'{dev["Manufacturer"]} {dev["Name"]}' if with_mfr else dev["Name"]
        # fractional-GPU sizes (g6f: 1/8..1/2 of an L4) report Count 0 (floored);
        # count the slice as 1 device -- gib already holds the slice's memory.
        count = max(dev["Count"], 1)
        return count, model, info[mem_key] / 1024
    return 0, None, None


def ebs_fields(api):
    """Return (base_iops, base_gbitps, peak_iops, peak_gbitps); 0 -> None."""
    if api is None:
        return None, None, None, None
    ebs = api.get("EbsInfo", {}).get("EbsOptimizedInfo")
    if not ebs:
        return None, None, None, None
    base_iops = ebs["BaselineIops"] or None
    base_bw = ebs["BaselineBandwidthInMbps"]
    peak_iops = ebs["MaximumIops"] or None
    peak_bw = ebs["MaximumBandwidthInMbps"]
    return (
        base_iops,
        base_bw / 1000 if base_bw else None,
        peak_iops,
        peak_bw / 1000 if peak_bw else None,
    )


def storage_fields(api):
    """Return (storage_gb, count, is_ssd, is_nvme) from InstanceStorageInfo."""
    if api is None or not api.get("InstanceStorageSupported"):
        return None, 0, None, None
    info = api["InstanceStorageInfo"]
    disk = info["Disks"][0]
    count = disk["Count"]
    return (
        disk["SizeInGB"] * count,
        count,
        disk["Type"] == "ssd",
        info["NvmeSupport"] in ("supported", "required"),
    )


def _memory_gib(api, price):
    # Use the price list's displayed memory (e.g. "1.7 GiB", "1,952 GiB"); this
    # is what AWS markets and matches the old build. Fall back to the API.
    mem = price.get("memory")
    if mem:
        try:
            return float(mem.split(" ")[0].replace(",", ""))
        except ValueError:
            pass
    if api is not None:
        return api["MemoryInfo"]["SizeInMiB"] / 1024.0
    return None


def _arch(api, price):
    if api is not None:
        return api["ProcessorInfo"]["SupportedArchitectures"][-1]
    proc = price.get("processor") or ""
    return "arm64" if "Graviton" in proc else "x86_64"


# Frozen hardware specs for previous-generation types that AWS still prices in
# us-east-1 but has removed from DescribeInstanceTypes in every region (verified:
# not via the paginator nor an explicit InstanceTypes=[...] lookup). There is no
# live API for these, so their specs are a one-time snapshot of the last good
# values (from the previous cloudspecs.duckdb). Commercial attributes
# (price, category, processor, current-generation flag, release date) stay live.
# Entries that are mostly null were already retired from the API before that
# snapshot was taken.
RETIRED_SPECS = {
    'cr1.8xlarge': {'vcpus': 32, 'cores': None, 'arch': 'x86_64', 'ram_gib': 244.0, 'net_gbitps': None, 'net_peak_gbitps': None, 'storage_gb': None, 'storage_count': 0, 'storage_is_ssd': None, 'storage_is_nvme': None, 'ebs_iops': None, 'ebs_gbitps': None, 'ebs_peak_iops': None, 'ebs_peak_gbitps': None, 'accelerators': 0, 'accelerator_model': None, 'accelerator_gib': None},
    'dl1.24xlarge': {'vcpus': 96, 'cores': 48, 'arch': 'x86_64', 'ram_gib': 768.0, 'net_gbitps': 100.0, 'net_peak_gbitps': 100.0, 'storage_gb': 4000, 'storage_count': 4, 'storage_is_ssd': True, 'storage_is_nvme': True, 'ebs_iops': 80000, 'ebs_gbitps': 19.0, 'ebs_peak_iops': 80000, 'ebs_peak_gbitps': 19.0, 'accelerators': 8, 'accelerator_model': 'Habana Gaudi HL-205', 'accelerator_gib': 256.0},
    'f1.16xlarge': {'vcpus': 64, 'cores': 32, 'arch': 'x86_64', 'ram_gib': 976.0, 'net_gbitps': 5.0, 'net_peak_gbitps': 20.0, 'storage_gb': 3760, 'storage_count': 4, 'storage_is_ssd': True, 'storage_is_nvme': True, 'ebs_iops': 75000, 'ebs_gbitps': 14.0, 'ebs_peak_iops': 75000, 'ebs_peak_gbitps': 14.0, 'accelerators': 8, 'accelerator_model': 'Xilinx Virtex UltraScale (VU9P)', 'accelerator_gib': 512.0},
    'f1.2xlarge': {'vcpus': 8, 'cores': 4, 'arch': 'x86_64', 'ram_gib': 122.0, 'net_gbitps': 2.5, 'net_peak_gbitps': 10.0, 'storage_gb': 470, 'storage_count': 1, 'storage_is_ssd': True, 'storage_is_nvme': True, 'ebs_iops': 12000, 'ebs_gbitps': 1.7, 'ebs_peak_iops': 12000, 'ebs_peak_gbitps': 1.7, 'accelerators': 1, 'accelerator_model': 'Xilinx Virtex UltraScale (VU9P)', 'accelerator_gib': 64.0},
    'f1.4xlarge': {'vcpus': 16, 'cores': 8, 'arch': 'x86_64', 'ram_gib': 244.0, 'net_gbitps': 5.0, 'net_peak_gbitps': 10.0, 'storage_gb': 940, 'storage_count': 1, 'storage_is_ssd': True, 'storage_is_nvme': True, 'ebs_iops': 44000, 'ebs_gbitps': 3.5, 'ebs_peak_iops': 44000, 'ebs_peak_gbitps': 3.5, 'accelerators': 2, 'accelerator_model': 'Xilinx Virtex UltraScale (VU9P)', 'accelerator_gib': 128.0},
    'g2.2xlarge': {'vcpus': 8, 'cores': None, 'arch': 'x86_64', 'ram_gib': 15.0, 'net_gbitps': None, 'net_peak_gbitps': None, 'storage_gb': None, 'storage_count': 0, 'storage_is_ssd': None, 'storage_is_nvme': None, 'ebs_iops': None, 'ebs_gbitps': None, 'ebs_peak_iops': None, 'ebs_peak_gbitps': None, 'accelerators': 0, 'accelerator_model': None, 'accelerator_gib': None},
    'g2.8xlarge': {'vcpus': 32, 'cores': None, 'arch': 'x86_64', 'ram_gib': 60.0, 'net_gbitps': None, 'net_peak_gbitps': None, 'storage_gb': None, 'storage_count': 0, 'storage_is_ssd': None, 'storage_is_nvme': None, 'ebs_iops': None, 'ebs_gbitps': None, 'ebs_peak_iops': None, 'ebs_peak_gbitps': None, 'accelerators': 0, 'accelerator_model': None, 'accelerator_gib': None},
    'g3.16xlarge': {'vcpus': 64, 'cores': None, 'arch': 'x86_64', 'ram_gib': 488.0, 'net_gbitps': None, 'net_peak_gbitps': None, 'storage_gb': None, 'storage_count': 0, 'storage_is_ssd': None, 'storage_is_nvme': None, 'ebs_iops': None, 'ebs_gbitps': None, 'ebs_peak_iops': None, 'ebs_peak_gbitps': None, 'accelerators': 0, 'accelerator_model': None, 'accelerator_gib': None},
    'g3.4xlarge': {'vcpus': 16, 'cores': None, 'arch': 'x86_64', 'ram_gib': 122.0, 'net_gbitps': None, 'net_peak_gbitps': None, 'storage_gb': None, 'storage_count': 0, 'storage_is_ssd': None, 'storage_is_nvme': None, 'ebs_iops': None, 'ebs_gbitps': None, 'ebs_peak_iops': None, 'ebs_peak_gbitps': None, 'accelerators': 0, 'accelerator_model': None, 'accelerator_gib': None},
    'g3.8xlarge': {'vcpus': 32, 'cores': None, 'arch': 'x86_64', 'ram_gib': 244.0, 'net_gbitps': None, 'net_peak_gbitps': None, 'storage_gb': None, 'storage_count': 0, 'storage_is_ssd': None, 'storage_is_nvme': None, 'ebs_iops': None, 'ebs_gbitps': None, 'ebs_peak_iops': None, 'ebs_peak_gbitps': None, 'accelerators': 0, 'accelerator_model': None, 'accelerator_gib': None},
    'g3s.xlarge': {'vcpus': 4, 'cores': None, 'arch': 'x86_64', 'ram_gib': 30.5, 'net_gbitps': None, 'net_peak_gbitps': None, 'storage_gb': None, 'storage_count': 0, 'storage_is_ssd': None, 'storage_is_nvme': None, 'ebs_iops': None, 'ebs_gbitps': None, 'ebs_peak_iops': None, 'ebs_peak_gbitps': None, 'accelerators': 0, 'accelerator_model': None, 'accelerator_gib': None},
    'i3.metal': {'vcpus': 72, 'cores': 36, 'arch': 'x86_64', 'ram_gib': 512.0, 'net_gbitps': 25.0, 'net_peak_gbitps': 25.0, 'storage_gb': 15200, 'storage_count': 8, 'storage_is_ssd': True, 'storage_is_nvme': True, 'ebs_iops': 80000, 'ebs_gbitps': 19.0, 'ebs_peak_iops': 80000, 'ebs_peak_gbitps': 19.0, 'accelerators': 0, 'accelerator_model': None, 'accelerator_gib': None},
    'p2.16xlarge': {'vcpus': 64, 'cores': None, 'arch': 'x86_64', 'ram_gib': 732.0, 'net_gbitps': None, 'net_peak_gbitps': None, 'storage_gb': None, 'storage_count': 0, 'storage_is_ssd': None, 'storage_is_nvme': None, 'ebs_iops': None, 'ebs_gbitps': None, 'ebs_peak_iops': None, 'ebs_peak_gbitps': None, 'accelerators': 0, 'accelerator_model': None, 'accelerator_gib': None},
    'p2.8xlarge': {'vcpus': 32, 'cores': None, 'arch': 'x86_64', 'ram_gib': 488.0, 'net_gbitps': None, 'net_peak_gbitps': None, 'storage_gb': None, 'storage_count': 0, 'storage_is_ssd': None, 'storage_is_nvme': None, 'ebs_iops': None, 'ebs_gbitps': None, 'ebs_peak_iops': None, 'ebs_peak_gbitps': None, 'accelerators': 0, 'accelerator_model': None, 'accelerator_gib': None},
    'p2.xlarge': {'vcpus': 4, 'cores': None, 'arch': 'x86_64', 'ram_gib': 61.0, 'net_gbitps': None, 'net_peak_gbitps': None, 'storage_gb': None, 'storage_count': 0, 'storage_is_ssd': None, 'storage_is_nvme': None, 'ebs_iops': None, 'ebs_gbitps': None, 'ebs_peak_iops': None, 'ebs_peak_gbitps': None, 'accelerators': 0, 'accelerator_model': None, 'accelerator_gib': None},
    'p3.16xlarge': {'vcpus': 64, 'cores': 32, 'arch': 'x86_64', 'ram_gib': 488.0, 'net_gbitps': 5.0, 'net_peak_gbitps': 10.0, 'storage_gb': None, 'storage_count': 0, 'storage_is_ssd': None, 'storage_is_nvme': None, 'ebs_iops': 80000, 'ebs_gbitps': 14.0, 'ebs_peak_iops': 80000, 'ebs_peak_gbitps': 14.0, 'accelerators': 8, 'accelerator_model': 'NVIDIA V100', 'accelerator_gib': 128.0},
    'p3.2xlarge': {'vcpus': 8, 'cores': 4, 'arch': 'x86_64', 'ram_gib': 61.0, 'net_gbitps': 2.5, 'net_peak_gbitps': 10.0, 'storage_gb': None, 'storage_count': 0, 'storage_is_ssd': None, 'storage_is_nvme': None, 'ebs_iops': 10000, 'ebs_gbitps': 1.75, 'ebs_peak_iops': 10000, 'ebs_peak_gbitps': 1.75, 'accelerators': 1, 'accelerator_model': 'NVIDIA V100', 'accelerator_gib': 16.0},
    'p3.8xlarge': {'vcpus': 32, 'cores': 16, 'arch': 'x86_64', 'ram_gib': 244.0, 'net_gbitps': 5.0, 'net_peak_gbitps': 10.0, 'storage_gb': None, 'storage_count': 0, 'storage_is_ssd': None, 'storage_is_nvme': None, 'ebs_iops': 40000, 'ebs_gbitps': 7.0, 'ebs_peak_iops': 40000, 'ebs_peak_gbitps': 7.0, 'accelerators': 4, 'accelerator_model': 'NVIDIA V100', 'accelerator_gib': 64.0},
    'u-12tb1.112xlarge': {'vcpus': 448, 'cores': 224, 'arch': 'x86_64', 'ram_gib': 12288.0, 'net_gbitps': 100.0, 'net_peak_gbitps': 100.0, 'storage_gb': None, 'storage_count': 0, 'storage_is_ssd': None, 'storage_is_nvme': None, 'ebs_iops': 160000, 'ebs_gbitps': 38.0, 'ebs_peak_iops': 160000, 'ebs_peak_gbitps': 38.0, 'accelerators': 0, 'accelerator_model': None, 'accelerator_gib': None},
    'u-18tb1.112xlarge': {'vcpus': 448, 'cores': 224, 'arch': 'x86_64', 'ram_gib': 18432.0, 'net_gbitps': 100.0, 'net_peak_gbitps': 100.0, 'storage_gb': None, 'storage_count': 0, 'storage_is_ssd': None, 'storage_is_nvme': None, 'ebs_iops': 160000, 'ebs_gbitps': 38.0, 'ebs_peak_iops': 160000, 'ebs_peak_gbitps': 38.0, 'accelerators': 0, 'accelerator_model': None, 'accelerator_gib': None},
    'u-24tb1.112xlarge': {'vcpus': 448, 'cores': 224, 'arch': 'x86_64', 'ram_gib': 24576.0, 'net_gbitps': 100.0, 'net_peak_gbitps': 100.0, 'storage_gb': None, 'storage_count': 0, 'storage_is_ssd': None, 'storage_is_nvme': None, 'ebs_iops': 160000, 'ebs_gbitps': 38.0, 'ebs_peak_iops': 160000, 'ebs_peak_gbitps': 38.0, 'accelerators': 0, 'accelerator_model': None, 'accelerator_gib': None},
    'u-9tb1.112xlarge': {'vcpus': 448, 'cores': 224, 'arch': 'x86_64', 'ram_gib': 9216.0, 'net_gbitps': 100.0, 'net_peak_gbitps': 100.0, 'storage_gb': None, 'storage_count': 0, 'storage_is_ssd': None, 'storage_is_nvme': None, 'ebs_iops': 160000, 'ebs_gbitps': 38.0, 'ebs_peak_iops': 160000, 'ebs_peak_gbitps': 38.0, 'accelerators': 0, 'accelerator_model': None, 'accelerator_gib': None},
}


# --------------------------------------------------------------------------- #
# Assemble rows
# --------------------------------------------------------------------------- #
# Column order must match the old cloudspecs.duckdb aws_all table exactly.
COLUMNS = [
    ("instance", "VARCHAR"), ("family", "VARCHAR"), ("category", "VARCHAR"),
    ("price_hour", "DOUBLE"), ("ram_gib", "DOUBLE"), ("vcpus", "BIGINT"),
    ("vcpus_base", "DOUBLE"), ("cores", "BIGINT"), ("processor_model", "VARCHAR"),
    ("arch", "VARCHAR"), ("net_gbitps", "DOUBLE"), ("net_peak_gbitps", "DOUBLE"),
    ("storage_gb", "BIGINT"), ("storage_count", "BIGINT"),
    ("storage_is_ssd", "BOOLEAN"), ("storage_is_nvme", "BOOLEAN"),
    ("ebs_iops", "BIGINT"), ("ebs_gbitps", "DOUBLE"), ("ebs_peak_iops", "BIGINT"),
    ("ebs_peak_gbitps", "DOUBLE"), ("accelerators", "BIGINT"),
    ("accelerator_model", "VARCHAR"), ("accelerator_gib", "DOUBLE"),
    ("is_current", "BOOLEAN"), ("storage_read_iops", "BIGINT"),
    ("storage_write_iops", "BIGINT"), ("release_year", "DOUBLE"),
]


def _hardware(itype, api, price):
    """Return the hardware-spec fields of a row as a dict.

    Priority: live DescribeInstanceTypes -> frozen RETIRED_SPECS snapshot ->
    price-list core fields only (hardware detail null).
    """
    if api is not None:
        s_gb, s_count, s_ssd, s_nvme = storage_fields(api)
        e_iops, e_bw, e_piops, e_pbw = ebs_fields(api)
        net_base, net_peak = network_bandwidth(api)
        acc_count, acc_model, acc_gib = accelerator_info(api)
        return {
            "vcpus": int(api["VCpuInfo"]["DefaultVCpus"]),
            "cores": int(api["VCpuInfo"]["DefaultCores"]),
            "arch": _arch(api, price),
            "ram_gib": _memory_gib(api, price),
            "net_gbitps": net_base, "net_peak_gbitps": net_peak,
            "storage_gb": s_gb, "storage_count": s_count,
            "storage_is_ssd": s_ssd, "storage_is_nvme": s_nvme,
            "ebs_iops": e_iops, "ebs_gbitps": e_bw,
            "ebs_peak_iops": e_piops, "ebs_peak_gbitps": e_pbw,
            "accelerators": acc_count, "accelerator_model": acc_model,
            "accelerator_gib": acc_gib,
        }
    if itype in RETIRED_SPECS:
        return dict(RETIRED_SPECS[itype])
    return {
        "vcpus": int(price["vcpu"]) if price.get("vcpu") else None,
        "cores": None, "arch": _arch(api, price), "ram_gib": _memory_gib(api, price),
        "net_gbitps": None, "net_peak_gbitps": None,
        "storage_gb": None, "storage_count": 0,
        "storage_is_ssd": None, "storage_is_nvme": None,
        "ebs_iops": None, "ebs_gbitps": None,
        "ebs_peak_iops": None, "ebs_peak_gbitps": None,
        "accelerators": 0, "accelerator_model": None, "accelerator_gib": None,
    }


def build_rows(described, pricing, releases, storage_iops):
    """Build one row (tuple in COLUMNS order) per priced instance type."""
    rows = []
    frozen = []
    no_specs = []
    for itype in sorted(pricing):
        price = pricing[itype]
        api = described.get(itype)
        if api is None:
            (frozen if itype in RETIRED_SPECS else no_specs).append(itype)

        hw = _hardware(itype, api, price)
        vcpus = hw["vcpus"]
        read_iops, write_iops = storage_iops.get(itype, (None, None))

        rows.append((
            itype,
            _family(itype),
            price["category"],
            price["price"],
            hw["ram_gib"],
            vcpus,
            vcpus_base(itype, vcpus),
            hw["cores"],
            price["processor"],
            hw["arch"],
            hw["net_gbitps"],
            hw["net_peak_gbitps"],
            hw["storage_gb"],
            hw["storage_count"],
            hw["storage_is_ssd"],
            hw["storage_is_nvme"],
            hw["ebs_iops"],
            hw["ebs_gbitps"],
            hw["ebs_peak_iops"],
            hw["ebs_peak_gbitps"],
            hw["accelerators"],
            hw["accelerator_model"],
            hw["accelerator_gib"],
            price["current"],
            read_iops,
            write_iops,
            releases.get(itype),
        ))
    if frozen:
        print(f"  NOTE: {len(frozen)} retired types not in DescribeInstanceTypes; "
              f"specs from frozen snapshot: {', '.join(frozen)}")
    if no_specs:
        print(f"  WARNING: {len(no_specs)} priced types have neither API nor frozen "
              f"specs (hardware detail null): {', '.join(no_specs)}")
    return rows


# --------------------------------------------------------------------------- #
# DuckDB output (views/comments ported verbatim from cloudspecs/toduckdb.sh)
# --------------------------------------------------------------------------- #
VIEWS_SQL = """
create view aws as
  select instance, family, category, price_hour, release_year, ram_gib, vcpus, cores, processor_model, arch, net_gbitps, net_peak_gbitps, storage_gb, storage_count, storage_read_iops, storage_write_iops, storage_is_ssd, storage_is_nvme, ebs_iops, ebs_gbitps, ebs_peak_iops, ebs_peak_gbitps
  from aws_all
  where vcpus_base = vcpus
  and is_current
  and instance not like 'hpc%'
  and instance not like '%metal%'
  and accelerators = 0
  and price_hour is not null
  and category not in ('GPU instance', 'FPGA Instances', 'Machine Learning ASIC Instances', 'Media Accelerator Instances');
create view aws_family as
  select * from aws join
  (select case when two.instance is null then one.instance
              when ((two.net_gbitps/two.price_hour)/(one.net_gbitps/one.price_hour) > 1.1) then two.instance
              else one.instance end as instance
  from (select * from (select *, row_number() over (partition by family order by vcpus desc) r from aws) where r = 1) one
  left join (select * from (select *, row_number() over (partition by family order by vcpus desc) r from aws) where r = 2) two
  using (family)) using (instance);
create view aws_accel as
  select *
  from aws_all
  where category in ('GPU instance', 'FPGA Instances', 'Machine Learning ASIC Instances', 'Media Accelerator Instances')
  and accelerator_model is not null;
create view aws_burst as
  select *
  from aws_all
  where vcpus_base != vcpus;
COMMENT ON COLUMN aws_all.instance IS 'Full name of the instance (e.g., c7g.medium)';
COMMENT ON COLUMN aws_all.price_hour IS 'us-east-1 on-demand price per hour in USD';
COMMENT ON COLUMN aws_all.cores IS 'Number of physical cores';
COMMENT ON COLUMN aws_all.vcpus IS 'Number of hyperthreads';
COMMENT ON COLUMN aws_all.ram_gib IS 'Amount of main memory in GiB';
COMMENT ON COLUMN aws_all.storage_gb IS 'Amount of instance storage in GB';
COMMENT ON COLUMN aws_all.net_gbitps IS 'Baseline network bandwidth after throttling';
COMMENT ON COLUMN aws_all.net_peak_gbitps IS 'Peak network bandwidth when bursting';
COMMENT ON COLUMN aws_all.storage_count IS 'Number of instance storage devices';
COMMENT ON COLUMN aws_all.arch IS 'Processor architecture';
COMMENT ON COLUMN aws_all.family IS 'Instance family (name prefix before the size, e.g. c7g)';
COMMENT ON COLUMN aws_all.category IS 'Instance category from the price list (e.g. General purpose, Compute optimized, GPU instance)';
COMMENT ON COLUMN aws_all.vcpus_base IS 'Baseline vCPUs (burstable/flex run below vcpus; equals vcpus otherwise)';
COMMENT ON COLUMN aws_all.processor_model IS 'Physical processor model (from the AWS price list)';
COMMENT ON COLUMN aws_all.storage_is_ssd IS 'Whether instance storage is SSD';
COMMENT ON COLUMN aws_all.storage_is_nvme IS 'Whether instance storage is NVMe';
COMMENT ON COLUMN aws_all.ebs_iops IS 'Baseline EBS-optimized IOPS';
COMMENT ON COLUMN aws_all.ebs_gbitps IS 'Baseline EBS-optimized bandwidth in Gbit/s';
COMMENT ON COLUMN aws_all.ebs_peak_iops IS 'Maximum (burst) EBS-optimized IOPS';
COMMENT ON COLUMN aws_all.ebs_peak_gbitps IS 'Maximum (burst) EBS-optimized bandwidth in Gbit/s';
COMMENT ON COLUMN aws_all.accelerators IS 'Number of attached accelerators (GPU/FPGA/Neuron/Media); fractional-GPU slices (g6f) count as 1';
COMMENT ON COLUMN aws_all.accelerator_model IS 'Accelerator model';
COMMENT ON COLUMN aws_all.accelerator_gib IS 'Total accelerator memory in GiB (API MiB value; the slice memory on fractional-GPU sizes)';
COMMENT ON COLUMN aws_all.is_current IS 'Whether this is a current-generation instance type';
COMMENT ON COLUMN aws_all.storage_read_iops IS 'Instance-storage random read IOPS';
COMMENT ON COLUMN aws_all.storage_write_iops IS 'Instance-storage random write IOPS';
COMMENT ON COLUMN aws_all.release_year IS 'Release date as a fractional year, year + (month-1)/12';
"""


def write_duckdb(rows, out_path):
    if os.path.exists(out_path):
        os.remove(out_path)
    con = duckdb.connect(out_path)
    cols_ddl = ", ".join(f'"{name}" {typ}' for name, typ in COLUMNS)
    con.execute(f"create table aws_all ({cols_ddl});")
    placeholders = ", ".join(["?"] * len(COLUMNS))
    con.executemany(f"insert into aws_all values ({placeholders})", rows)
    con.execute(
        f"create table benchmark as select * from read_csv('{HERE}/benchmark.csv');"
    )
    con.execute(VIEWS_SQL)
    counts = {
        v: con.execute(f"select count(*) from {v}").fetchone()[0]
        for v in ("aws_all", "aws", "aws_family", "aws_accel", "aws_burst", "benchmark")
    }
    con.close()
    print("Wrote " + out_path)
    for v, c in counts.items():
        print(f"  {v}: {c}")


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--output", default=os.path.join(HERE, "cloudspecs.duckdb"))
    ap.add_argument("--refresh", action="store_true",
                    help="re-download cached inputs (price list, timeline, docs)")
    args = ap.parse_args()

    if args.refresh:
        for p in (PRICE_CACHE, TIMELINE_CACHE):
            if os.path.exists(p):
                os.remove(p)
        for page in STORAGE_DOC_PAGES:
            p = os.path.join(WORK, f"{page}.html")
            if os.path.exists(p):
                os.remove(p)

    pricing = load_pricing()
    described = describe_instance_types()
    releases = load_release_years()
    storage_iops = load_storage_iops()

    print("Building rows ...")
    rows = build_rows(described, pricing, releases, storage_iops)
    write_duckdb(rows, args.output)


if __name__ == "__main__":
    sys.exit(main())
