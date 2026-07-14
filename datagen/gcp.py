#!/usr/bin/env python3
"""
Get Google Cloud (GCP) Compute Engine instance data.

Standalone GCP counterpart to build.py. Collects machine-type specs and the
Linux on-demand price in one lowest-price-tier region (us-central1), and writes a
`gcp_all` table + views into DuckDB. Columns match aws_all exactly, so the two
clouds are directly comparable.

Output objects (mirroring the AWS build):
  gcp_all    every machine type, all 27 aws_all columns.
  gcp        comparable slice: current, priced, non-shared-core, non-accelerator,
             non-metal, non-TPU (the same "strange instances" the AWS aws view drops).
  gcp_family one representative machine type per family (largest, or 2nd-largest if
             >1.1x more network-efficient per dollar); mirrors aws_family.
  gcp_accel  accelerator (GPU) machine types.
  gcp_shared  shared-core machine types (vcpus_base < vcpus).

Data sources:
  1. Compute Engine API  (compute.googleapis.com)  -> machine specs
       aggregated/machineTypes: vCPU, memory, architecture, shared-cpu, GPU,
       bundled local SSD, deprecation state.
  2. Cloud Billing Catalog API  (cloudbilling.googleapis.com)  -> pricing
       services/<compute>/skus: per-family Core/Ram $/hour + local-SSD $/GiB-month
       rates for the region. GCP has no per-machine price; the on-demand hourly
       price is assembled as core_rate * vCPU + ram_rate * RAM_GiB, plus the
       bundled local-SSD cost (storage_gb * ssd_rate / 730) and the attached-GPU
       cost (accelerators * gpu_rate) where those SKUs exist.
  3. GCP machine-family docs (cloud.google.com/compute/docs/*-machines) ->
       per-machine-type default & Tier_1 egress bandwidth (Gbps).

Derived / static: physical cores (vCPU / threads-per-core), local-SSD IOPS
(partition count x per-partition NVMe rate), vcpus_base (shared-core baseline),
processor_model (family CPU platform), release year (family GA-date table).

Auth: a service-account JSON key (default ../gcp.json, or
$GOOGLE_APPLICATION_CREDENTIALS). Requires BOTH APIs enabled and the service
account granted Viewer (or compute.readonly + billing viewer):
  - https://console.cloud.google.com/apis/library/compute.googleapis.com
  - https://console.cloud.google.com/apis/library/cloudbilling.googleapis.com

Notes vs AWS: processor_model is the family CPU platform (e.g. "AMD EPYC Genoa"),
coarser than AWS's exact model and "variable" for families spanning platforms
(e2, n1). ebs_* is always NULL -- GCP's Hyperdisk families provision disk
performance per-disk with no published per-machine-type cap. Accelerator families
(in gcp_accel) get an all-in price including the attached GPUs; types whose GPU
(or CPU/RAM) has no on-demand SKU in the region -- A4/A4X B200/GB200/GB300, TPUs,
h100-mega -- are left null rather than priced CPU+RAM only.
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
import jwt  # PyJWT (with cryptography for RS256)
from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_CREDS = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS", os.path.join(HERE, "..", "gcp.json")
)
DEFAULT_REGION = "us-central1"  # GCP's reference region; change with --region

SCOPES = (
    "https://www.googleapis.com/auth/compute.readonly "
    "https://www.googleapis.com/auth/cloud-billing.readonly"
)
COMPUTE_API = "https://compute.googleapis.com/compute/v1"
BILLING_API = "https://cloudbilling.googleapis.com/v1"
COMPUTE_SERVICE_ID = "6F81-5844-456A"  # Compute Engine, for the billing catalog

# Per-machine-type network bandwidth lives only in the machine-family doc pages.
DOC_BASE = "https://cloud.google.com/compute/docs/"
MACHINE_DOC_PAGES = [
    "general-purpose-machines", "compute-optimized-machines",
    "memory-optimized-machines", "accelerator-optimized-machines",
    "storage-optimized-machines",
]

# GCP bundled local SSD (cloud.google.com/compute/docs/disks/local-ssd).
# The API's bundledLocalSsds.partitionCount is the physical disk count. Physical
# disk SIZE and per-disk IOPS vary by family, so capacity and IOPS cannot be
# derived from partitionCount alone:
#   * Standard local SSD (375 GiB disks): C3, C3D, A2/A3/A4/A4X and older series.
#     Per-VM IOPS follow a capped, non-linear table keyed by disk count.
#   * Titanium SSD: C4/C4A/C4D/G4/H4D use 375 GiB disks (3 TiB on bare metal) and
#     scale linearly at 150k/75k IOPS per 375 GiB. Z3 uses 3 TiB disks (6 TiB on
#     metal), each disk capped at 750k/500k IOPS regardless of disk size.
LOCAL_SSD_DEFAULT_GIB = 375
# families whose physical disk is larger than 375 GiB
Z3_DISK_GIB = 3000            # Z3 uses 3 TiB local SSD disks
Z3_METAL_DISK_GIB = 6000      # Z3 bare metal uses 6 TiB disks
C4_METAL_DISK_GIB = 3000      # C4/C4D bare metal bundle 3 TiB disks
# Titanium families that scale linearly with capacity, IOPS per 375 GiB unit
TITANIUM_IOPS_PER_375 = {
    "c4": (150000, 75000), "c4a": (150000, 75000), "c4d": (150000, 75000),
    "g4": (150000, 75000), "h4d": (150000, 75000),
}
# Z3 Titanium: fixed IOPS per physical disk (independent of the 3/6 TiB disk size)
Z3_IOPS_PER_DISK = (750000, 500000)
# Standard NVMe local SSD: documented per-VM IOPS by disk count (capped, non-linear)
NVME_IOPS_BY_DISKS = {
    1: (170000, 90000), 2: (340000, 180000), 3: (510000, 270000),
    4: (680000, 360000), 8: (680000, 360000),
    16: (1600000, 800000), 24: (2400000, 1200000), 32: (3200000, 1600000),
}


def _local_ssd(family, name, parts):
    """(storage_gb, read_iops, write_iops) for `parts` bundled local SSD disks."""
    if not parts:
        return None, None, None
    metal = "metal" in name
    if family == "z3":
        disk_gib = Z3_METAL_DISK_GIB if metal else Z3_DISK_GIB
        r, w = Z3_IOPS_PER_DISK
        return parts * disk_gib, parts * r, parts * w
    disk_gib = C4_METAL_DISK_GIB if (metal and family in ("c4", "c4d")) else LOCAL_SSD_DEFAULT_GIB
    gb = parts * disk_gib
    if family in TITANIUM_IOPS_PER_375:                 # capacity-linear per 375 GiB
        r, w = TITANIUM_IOPS_PER_375[family]
        units = gb // LOCAL_SSD_DEFAULT_GIB
        return gb, units * r, units * w
    k = max((d for d in NVME_IOPS_BY_DISKS if d <= parts), default=1)   # standard NVMe
    r, w = NVME_IOPS_BY_DISKS[k]
    return gb, r, w

# x86 families that ship with simultaneous multithreading OFF (1 vCPU = 1 core);
# Arm families are detected via the API's architecture field instead.
NO_SMT_FAMILIES = {"t2d", "h3"}

# GA date per machine family (no API exposes this). (year, month) where the GA
# month is confirmed from Google Cloud announcements/release notes; a bare year
# where only the year is known (legacy or newest families). Emitted as a
# fractional year -- year + (month-1)/12 -- matching the AWS build's format.
_GA_DATE = {
    "n1": 2015, "f1": 2013, "g1": 2013,                    # legacy, year only
    "c2": (2019, 8), "n2": (2019, 9),                      # 2019
    "e2": (2020, 3), "n2d": (2020, 4), "m1": 2018, "m2": 2020,
    "a2": (2021, 3), "c2d": (2021, 9), "t2d": (2021, 11),  # 2021
    "t2a": (2022, 10), "m3": (2022, 10),                   # 2022
    "c3": (2023, 5), "g2": (2023, 5), "h3": (2023, 8),     # 2023
    "a3": (2023, 9), "c3d": (2023, 10), "z3": (2023, 11),
    "n4": (2024, 5), "c4": (2024, 8), "c4a": (2024, 10), "x4": 2024,  # 2024
    "c4d": (2025, 6), "h4d": 2025, "m4": 2025, "n4a": 2025,  # 2025 (newest)
    "n4d": 2025, "c4n": 2025, "a4": 2025, "a4x": 2025, "g4": 2025,
}


def _release_year(family):
    """Fractional GA year for a family (month precision where known)."""
    d = _GA_DATE.get(family)
    if d is None:
        return None
    if isinstance(d, tuple):
        return round(d[0] + (d[1] - 1) / 12, 2)
    return float(d)

# GCP prices vCPU/RAM per machine-family "group", but names the group
# inconsistently in the SKU description. Most families use a token
# ("N2 Instance Core running in ...", "C4A Arm Instance Ram running in ...");
# three legacy groups use a category phrase instead. We extract the group phrase
# from the description and canonicalise it to a family key (see _group_key), and
# map each machine type's name prefix to the same key (see _instance_key).
_LEGACY_GROUP = {                 # category-named SKU groups -> family key
    "compute optimized": "C2", "compute-optimized": "C2",
    "memory optimized": "M1M2", "memory-optimized": "M1M2",
    "n1 predefined": "N1",
}
_INSTANCE_ALIAS = {               # machine-name prefix -> family key (non-identity)
    "C2": "C2", "M1": "M1M2", "M2": "M1M2", "N1": "N1",
}
# SKU descriptions that are add-ons / non-baseline and must never contribute to
# on-demand CPU/RAM rates (spot & commitment are already excluded by usageType).
_PRICE_SKIP = (
    "sole tenancy", "sole-tenancy", "premium", "reserved", "reservation",
    "commitment", "sustained", "custom", "dws", "vm state", "upgrade",
    "flex start", "calendar mode", "defined duration",
)
# resource keyword at the end of a description (after stripping " running in ...")
_RESOURCE_RE = re.compile(r"\s*(instance\s+)?(core|ram)$", re.I)
# bundled local-SSD add-on SKU, e.g. "C4 Instance Local SSD" -> group "C4"
_LOCALSSD_RE = re.compile(r"\s*(instance\s+)?local ssd$", re.I)
# GCP converts $/GiB-month storage rates to hourly using a 730-hour month.
HOURS_PER_MONTH = 730.0

# arm64 machine families (fallback when the API omits the architecture field).
ARM_FAMILIES = {"t2a", "c4a", "a4", "a4x"}

# Shared-core baseline vCPUs (GCP's burstable analog). Everything else runs at
# its full vCPU count, so vcpus_base == vcpus.
SHARED_CORE_BASE = {
    "e2-micro": 0.25, "e2-small": 0.5, "e2-medium": 1.0,
    "f1-micro": 0.2, "g1-small": 0.5,
}

# CPU platform per family -- GCP's coarser analog of AWS processor_model (a
# family-level platform, not an exact SKU; "variable" where a family spans
# several). Sourced from cloud.google.com/compute/docs/cpu-platforms. Newest /
# uncertain families (c4n, m4, f1, g1) are left out -> null.
CPU_PLATFORM = {
    "n1": "Intel Skylake", "n2": "Intel Cascade Lake, Ice Lake",
    "n2d": "AMD EPYC Rome, Milan", "n4": "Intel Emerald Rapids",
    "n4a": "Google Axion", "n4d": "AMD EPYC Turin", "e2": "Intel/AMD (variable)",
    "t2d": "AMD EPYC Milan", "t2a": "Ampere Altra",
    "c2": "Intel Cascade Lake", "c2d": "AMD EPYC Milan",
    "c3": "Intel Sapphire Rapids", "c3d": "AMD EPYC Genoa",
    "c4": "Intel Emerald Rapids, Granite Rapids", "c4a": "Google Axion",
    "c4d": "AMD EPYC Turin", "h3": "Intel Sapphire Rapids", "h4d": "AMD EPYC Turin",
    "m1": "Intel Skylake, Broadwell", "m2": "Intel Cascade Lake",
    "m3": "Intel Ice Lake", "x4": "Intel Sapphire Rapids",
    "a2": "Intel Cascade Lake", "a3": "Intel Sapphire Rapids",
    "a4": "Intel Emerald Rapids", "a4x": "NVIDIA Grace",
    "g2": "Intel Cascade Lake", "g4": "AMD EPYC Turin", "z3": "Intel Sapphire Rapids",
}

# Per-accelerator memory (GiB, nominal) by guestAcceleratorType, from the Google
# Cloud GPU/TPU docs (the API does not report accelerator memory). TPU entries are
# HBM per chip. Unknown models -> 0 (field left null rather than faked).
GPU_MEM_GIB = {
    "nvidia-gb300": 288,
    "nvidia-gb200": 186,
    "nvidia-b200": 180,
    "nvidia-h200-141gb": 141,
    "nvidia-h100-80gb": 80,
    "nvidia-h100-mega-80gb": 80,
    "nvidia-a100-80gb": 80,
    "nvidia-tesla-a100": 40,
    "nvidia-l4": 24,
    "nvidia-rtx-pro-6000": 96,
    "ct3": 32, "ct3p": 32,          # TPU v3
    "ct5l": 16, "ct5lp": 16,        # TPU v5e
    "ct5p": 95,                     # TPU v5p
    "ct6e": 32,                     # TPU v6e (Trillium)
    "tpu7x": 192,                   # TPU v7 (Ironwood)
}

# guestAcceleratorType -> on-demand GPU SKU description (minus " running in ...").
# The attached GPU is a separate mandatory SKU ($/GPU-hour); add accelerators * rate
# to price_hour so accelerator types get an all-in price (as AWS bundles the GPU).
# Models with no plain on-demand SKU in the region (h100-mega, B200/GB200/GB300,
# TPUs) are omitted -> those rows can't be fully priced and are left null.
GPU_SKU = {
    "nvidia-tesla-a100":   "Nvidia Tesla A100 GPU",
    "nvidia-a100-80gb":    "Nvidia Tesla A100 80GB GPU",
    "nvidia-h100-80gb":    "Nvidia H100 80GB GPU",
    "nvidia-h200-141gb":   "H200 141GB GPU",
    "nvidia-l4":           "Nvidia L4 GPU",
    "nvidia-rtx-pro-6000": "RTX 6000 96GB",
}

# guestAcceleratorType -> display name matching the other clouds' style ("NVIDIA
# H100", not "nvidia-h100-80gb"), applied on write; the slug keys GPU_MEM_GIB /
# GPU_SKU internally. Memory variants share a name -- accelerator_gib disambiguates.
ACCEL_MODEL = {
    "nvidia-tesla-a100":   "NVIDIA A100",
    "nvidia-a100-80gb":    "NVIDIA A100",
    "nvidia-h100-80gb":    "NVIDIA H100",
    "nvidia-h100-mega-80gb": "NVIDIA H100",
    "nvidia-h200-141gb":   "NVIDIA H200",
    "nvidia-b200":         "NVIDIA B200",
    "nvidia-gb200":        "NVIDIA GB200",
    "nvidia-gb300":        "NVIDIA GB300",
    "nvidia-l4":           "NVIDIA L4",
    "nvidia-rtx-pro-6000": "NVIDIA RTX PRO 6000",
    "ct3": "Google TPU v3", "ct3p": "Google TPU v3",
    "ct5l": "Google TPU v5e", "ct5lp": "Google TPU v5e",
    "ct5p": "Google TPU v5p",
    "ct6e": "Google TPU v6e",
    "tpu7x": "Google TPU v7",
}


def _group_key(group):
    """Canonical family key from a SKU group phrase (e.g. 'C4A Arm' -> 'C4A')."""
    g = group.strip().lower()
    if g in _LEGACY_GROUP:
        return _LEGACY_GROUP[g]
    return g.split()[0].upper() if g else None


def _instance_key(name):
    """Canonical family key from a machine-type name (e.g. 'c2-standard-8')."""
    tok = name.split("-")[0].upper()
    return _INSTANCE_ALIAS.get(tok, tok)


# Shared-core size words (e2-micro etc. carry no numeric size token).
_SHARED_CORE_SIZES = {"nano", "micro", "small", "medium", "large"}


def _family(name):
    """AWS-like family: the machine-type name minus its size token, so the shape /
    memory-ratio stays part of the family (c2-standard-30 -> c2-standard;
    z3-highmem-14-standardlssd -> z3-highmem-standardlssd; a2-highgpu-1g ->
    a2-highgpu; e2-micro -> e2). The size token is the vCPU count, else an
    accelerator/TPU count (\\d+g / \\d+t), else a shared-core size word."""
    toks = name.split("-")
    idx = next((i for i, t in enumerate(toks) if t.isdigit()), None)
    if idx is None:
        idx = next((i for i, t in enumerate(toks) if re.fullmatch(r"\d+[a-z]", t)), None)
    if idx is None:
        idx = next((i for i, t in enumerate(toks) if t in _SHARED_CORE_SIZES), None)
    if idx is None:
        return name
    return "-".join(toks[:idx] + toks[idx + 1:])


def _arch(architecture, name):
    """Resolve arch from the API architecture field, else the family name."""
    if architecture == "ARM64":
        return "arm64"
    if architecture == "X86_64":
        return "x86_64"
    return "arm64" if name.split("-")[0] in ARM_FAMILIES else "x86_64"


def _physical_cores(vcpus, arch, family, shared):
    """vCPU / threads-per-core; None for shared-core (fractional) types."""
    if shared:
        return None
    threads = 1 if (arch == "arm64" or family in NO_SMT_FAMILIES) else 2
    return max(1, vcpus // threads)


# --------------------------------------------------------------------------- #
# Auth + HTTP
# --------------------------------------------------------------------------- #
def get_access_token(creds):
    now = int(time.time())
    assertion = jwt.encode(
        {"iss": creds["client_email"], "scope": SCOPES,
         "aud": creds["token_uri"], "iat": now, "exp": now + 3600},
        creds["private_key"], algorithm="RS256",
    )
    body = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": assertion,
    }).encode()
    resp = urllib.request.urlopen(urllib.request.Request(creds["token_uri"], data=body))
    return json.load(resp)["access_token"]


def api_get(url, token):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        if e.code == 403 and "SERVICE_DISABLED" in detail:
            host = urllib.parse.urlparse(url).netloc
            sys.exit(
                f"\nERROR: the API at {host} is not enabled on this GCP project.\n"
                f"Enable it, wait a minute, and retry:\n"
                f"  https://console.cloud.google.com/apis/library/{host}\n"
            )
        raise SystemExit(f"HTTP {e.code} for {url}\n{detail[:500]}")


def api_paginate(base_url, token, page_key="pageToken"):
    """Yield each page of a paginated Google API list endpoint."""
    token_val = ""
    while True:
        sep = "&" if "?" in base_url else "?"
        url = base_url + (f"{sep}{page_key}={token_val}" if token_val else "")
        page = api_get(url, token)
        yield page
        token_val = page.get("nextPageToken", "")
        if not token_val:
            return


# --------------------------------------------------------------------------- #
# Machine specs (Compute Engine API)
# --------------------------------------------------------------------------- #
def determine_category(name):
    """Map a machine-type name to a human category (ported from upstream)."""
    n = name.lower()
    if any(k in n for k in ("highmem", "megamem", "ultramem")) or n[:3] in (
        "m1-", "m2-", "m3-", "m4-", "x4-"):
        return "Memory optimized"
    if n.startswith(("c2-", "c2d-", "c3-", "c3d-", "c4-", "c4a-", "c4d-", "h3-")) \
            or "highcpu" in n:
        return "Compute optimized"  # highmem already returned above
    if n.startswith(("a2-", "a3-", "a4-", "a4x-", "g2-", "g4-")):
        return "Accelerator optimized"
    if n.startswith("z3-"):
        return "Storage optimized"
    return "General purpose"


def fetch_machine_specs(token, project, region):
    """Return {machine_type: specs} for machine types available in `region`."""
    print(f"Fetching machine types (region {region}) ...")
    specs = {}
    zones = {}
    url = f"{COMPUTE_API}/projects/{project}/aggregated/machineTypes?maxResults=500"
    for page in api_paginate(url, token):
        for zone_path, scoped in page.get("items", {}).items():
            zone = zone_path.split("/")[-1]
            for mt in scoped.get("machineTypes", []) or []:
                name = mt["name"]
                if "custom" in name:
                    continue
                zones.setdefault(name, set()).add(zone)
                if name in specs:
                    continue
                series = name.split("-")[0]   # coarse key for platform/IOPS/GA lookups
                vcpus = mt["guestCpus"]
                shared = mt.get("isSharedCpu", False)
                arch = _arch(mt.get("architecture"), name)

                gpu = mt.get("accelerators") or []
                model = gpu[0]["guestAcceleratorType"] if gpu else None
                count = gpu[0]["guestAcceleratorCount"] if gpu else 0

                # bundled local SSD: partitionCount = physical disk count; disk size
                # and IOPS vary by family (see _local_ssd). None when unbundled.
                lssd = mt.get("bundledLocalSsds") or {}
                parts = lssd.get("partitionCount") or 0
                storage_gb, ssd_read_iops, ssd_write_iops = _local_ssd(series, name, parts)

                dep = mt.get("deprecated") or {}
                current = dep.get("state") not in ("DEPRECATED", "OBSOLETE", "DELETED")

                specs[name] = {
                    "family": _family(name),
                    "vcpus": vcpus,
                    "vcpus_base": SHARED_CORE_BASE.get(name, float(vcpus)),
                    "ram_gib": mt["memoryMb"] / 1024.0,
                    "cores": _physical_cores(vcpus, arch, series, shared),
                    "processor_model": CPU_PLATFORM.get(series),
                    "arch": arch,
                    "category": determine_category(name),
                    "accelerators": count,
                    "accelerator_model": model,
                    "accelerator_gib": (count * GPU_MEM_GIB.get(model, 0)) or None,
                    "storage_gb": storage_gb,
                    "storage_count": parts,
                    "storage_is_ssd": True if parts else None,
                    "storage_is_nvme": (lssd.get("defaultInterface") == "NVME") if parts else None,
                    "storage_read_iops": ssd_read_iops,
                    "storage_write_iops": ssd_write_iops,
                    "is_current": current,
                    "release_year": _release_year(series),
                }
    kept = {}
    for name, spec in specs.items():
        if any(z.rsplit("-", 1)[0] == region for z in zones.get(name, ())):
            kept[name] = spec
    print(f"  {len(kept)} machine types available in {region} "
          f"({len(specs)} total across all regions)")
    return kept


# --------------------------------------------------------------------------- #
# Network bandwidth (machine-family doc pages)
# --------------------------------------------------------------------------- #
def _doc_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", errors="replace")


def _parse_gbps(text):
    """'Up to 10' / '100' -> float; 'N/A' / '' -> None."""
    if not text:
        return None
    m = re.search(r"[\d.]+", text.replace(",", ""))
    return float(m.group()) if m else None


def fetch_network():
    """Return {machine_type: (default_gbps, tier1_gbps)} scraped from GCP docs."""
    print("Scraping per-machine-type network bandwidth from GCP docs ...")
    net = {}
    for page in MACHINE_DOC_PAGES:
        try:
            soup = BeautifulSoup(_doc_get(DOC_BASE + page), "html.parser")
        except Exception as e:
            print(f"  WARNING: {page} failed ({e})")
            continue
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            # The header row contains "Machine type[s]"; some tables (accelerator
            # pages) put a spanning group label above it, so scan the first 2 rows.
            header = None
            hdr_idx = 0
            for idx in range(min(2, len(rows))):
                cells = [c.get_text(" ", strip=True).lower() for c in rows[idx].find_all(["th", "td"])]
                if any("machine type" in h for h in cells):
                    header, hdr_idx = cells, idx
                    break
            if header is None:
                continue

            def col(*subs):
                for i, h in enumerate(header):
                    if all(s in h for s in subs):
                        return i
                return None

            mt_col = col("machine type")
            # egress column names vary: "Default/Maximum egress bandwidth" on most
            # pages, "Maximum network bandwidth" on the accelerator pages.
            def_col = next((c for c in (col("default egress"), col("maximum egress"),
                                        col("maximum network"), col("network bandwidth"))
                            if c is not None), None)
            t1_col = col("tier_1 egress")
            if mt_col is None or (def_col is None and t1_col is None):
                continue
            for row in rows[hdr_idx + 1:]:
                cells = [c.get_text(" ", strip=True) for c in row.find_all(["th", "td"])]
                if mt_col >= len(cells):
                    continue
                names = re.findall(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+", cells[mt_col].lower())
                if not names:
                    continue
                default = _parse_gbps(cells[def_col]) if def_col is not None and def_col < len(cells) else None
                tier1 = _parse_gbps(cells[t1_col]) if t1_col is not None and t1_col < len(cells) else None
                for nm in names:
                    net[nm] = (default, tier1)
    print(f"  network bandwidth for {len(net)} machine types")
    return net


# --------------------------------------------------------------------------- #
# Pricing (Cloud Billing Catalog API)
# --------------------------------------------------------------------------- #
def _hourly_rate(sku):
    """USD hourly rate from a Core/Ram SKU (per vCPU-hour or per GiB-hour)."""
    infos = sku.get("pricingInfo") or []
    if not infos:
        return None
    tiers = infos[-1].get("pricingExpression", {}).get("tieredRates") or []
    if not tiers:
        return None
    unit_price = tiers[0].get("unitPrice", {})
    if unit_price.get("currencyCode") != "USD":
        return None
    return int(unit_price.get("units", "0") or 0) + (unit_price.get("nanos", 0) or 0) / 1e9


def fetch_rates(token, region):
    """Return (core_rates, ram_rates, ssd_rates, gpu_rates) for `region`. core/ram
    are {family_key: $/unit-hour}; ssd is {family_key: $/GiB-month} plus a "_default"
    (generic "SSD backed Local Storage" rate); gpu is {SKU description: $/GPU-hour}."""
    print("Fetching Compute Engine pricing catalog ...")
    core = {}
    ram = {}
    ssd = {}
    gpu = {}
    n_skus = 0
    url = f"{BILLING_API}/services/{COMPUTE_SERVICE_ID}/skus?pageSize=5000"
    for page in api_paginate(url, token):
        for sku in page.get("skus", []):
            n_skus += 1
            cat = sku.get("category", {})
            if cat.get("usageType") != "OnDemand":
                continue
            if region not in (sku.get("serviceRegions") or []):
                continue
            desc = sku.get("description", "")
            low = desc.lower()
            if any(s in low for s in _PRICE_SKIP):
                continue
            # generic standard local-SSD storage SKU (Storage family) -> default rate
            if low == "ssd backed local storage":
                rate = _hourly_rate(sku)
                if rate is not None:
                    ssd["_default"] = rate
                continue
            base = re.sub(r"\s+running in .*$", "", desc, flags=re.I).strip()
            # attached-GPU add-on SKU ($/GPU-hour), keyed by SKU description
            if cat.get("resourceGroup") == "GPU":
                rate = _hourly_rate(sku)
                if rate is not None:
                    gpu[base] = rate
                continue
            if cat.get("resourceFamily") != "Compute":
                continue
            # bundled local-SSD add-on, e.g. "C4 Instance Local SSD" -> group "C4"
            m = _LOCALSSD_RE.search(base)
            if m:
                key = _group_key(base[: m.start()])
                rate = _hourly_rate(sku)
                if key and rate is not None:
                    ssd[key] = rate
                continue
            # "N2 Instance Core running in Iowa" -> group "N2", resource "core"
            m = _RESOURCE_RE.search(base)
            if not m:
                continue
            resource = m.group(2).lower()
            key = _group_key(base[: m.start()])
            if not key:
                continue
            rate = _hourly_rate(sku)
            if rate is None:
                continue
            (core if resource == "core" else ram)[key] = rate
    fams = sorted(set(core) | set(ram))
    print(f"  scanned {n_skus} SKUs; rates for {len(fams)} families in {region}: "
          f"{', '.join(fams)}")
    print(f"  local-SSD $/GiB-month: default {ssd.get('_default')}, "
          f"specific {sorted(k for k in ssd if k != '_default')}")
    resolved = sorted(m for m, d in GPU_SKU.items() if d in gpu)
    print(f"  GPU $/hour: {len(gpu)} SKUs; priceable models {resolved}")
    return core, ram, ssd, gpu


# --------------------------------------------------------------------------- #
# Assemble
# --------------------------------------------------------------------------- #
# Columns match aws_all exactly (same names, same order), so gcp_all is directly
# comparable to aws_all. ebs_* are kept for parity but always null (GCP has no
# published per-machine-type disk cap for its Hyperdisk families).
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


def build_rows(specs, net, core_rates, ram_rates, ssd_rates, gpu_rates, region):
    rows = []
    no_price = []
    gpu_priced = 0
    gpu_unpriced = []
    for name in sorted(specs):
        s = specs[name]
        key = _instance_key(name)
        cr = core_rates.get(key)
        rr = ram_rates.get(key)
        price = cr * s["vcpus"] + rr * s["ram_gib"] if cr is not None and rr is not None else None
        # add mandatory bundled local-SSD cost (separate GCP SKU, $/GiB-month), so
        # price_hour is the all-in on-demand price -- matches AWS, where instance
        # storage is bundled into the instance price.
        if price is not None and s["storage_gb"]:
            ssd_rate = ssd_rates.get(key, ssd_rates.get("_default"))
            if ssd_rate:
                price += s["storage_gb"] * ssd_rate / HOURS_PER_MONTH
        # add mandatory attached-GPU cost (separate GCP SKU, $/GPU-hour). When the
        # model has no on-demand GPU SKU in the region, a CPU+RAM(+SSD) figure would
        # understate the true price -- null it rather than emit a misleading number.
        if price is not None and s["accelerators"]:
            sku = GPU_SKU.get(s["accelerator_model"])
            gpu_rate = gpu_rates.get(sku) if sku else None
            if gpu_rate is not None:
                price += s["accelerators"] * gpu_rate
                gpu_priced += 1
            else:
                price = None
                gpu_unpriced.append(name)
        if price is None:
            no_price.append(name)

        net_base, net_tier1 = net.get(name, (None, None))
        net_peak = net_tier1 if net_tier1 is not None else net_base

        rows.append((
            name,
            s["family"],
            s["category"],
            price,
            s["ram_gib"],
            s["vcpus"],
            s["vcpus_base"],
            s["cores"],
            s["processor_model"],
            s["arch"],
            net_base,
            net_peak,
            s["storage_gb"],
            s["storage_count"],
            s["storage_is_ssd"],
            s["storage_is_nvme"],
            None,  # ebs_iops        -- GCP Hyperdisk is provisioned per-disk,
            None,  # ebs_gbitps         no published per-machine-type cap
            None,  # ebs_peak_iops
            None,  # ebs_peak_gbitps
            s["accelerators"],
            ACCEL_MODEL.get(s["accelerator_model"], s["accelerator_model"]),
            s["accelerator_gib"],
            s["is_current"],
            s["storage_read_iops"],
            s["storage_write_iops"],
            s["release_year"],
        ))
    if no_price:
        print(f"  NOTE: {len(no_price)} machine types have no on-demand core/ram "
              f"rate in {region} (price null): {', '.join(no_price[:20])}"
              + (" ..." if len(no_price) > 20 else ""))
    n_net = sum(1 for r in rows if r[10] is not None)
    print(f"  network bandwidth matched for {n_net}/{len(rows)} machine types")
    print(f"  {gpu_priced} accelerator machine types priced all-in (CPU+RAM+GPU)")
    if gpu_unpriced:
        print(f"  NOTE: {len(gpu_unpriced)} accelerator types have no on-demand GPU "
              f"SKU in {region} (price null): {', '.join(gpu_unpriced)}")
    return rows


# Base table `gcp_all` (all machine types) + views, mirroring the AWS build.
# `gcp` is the comparable slice: current, priced, non-shared-core, non-accelerator,
# non-metal, non-TPU -- i.e. the same "strange instances" the AWS `aws` view drops.
VIEWS_SQL = """
create view gcp as
  select instance, family, category, price_hour, release_year, ram_gib, vcpus, cores, processor_model, arch, net_gbitps, net_peak_gbitps, storage_gb, storage_count, storage_read_iops, storage_write_iops, storage_is_ssd, storage_is_nvme, ebs_iops, ebs_gbitps, ebs_peak_iops, ebs_peak_gbitps
  from gcp_all
  where vcpus_base = vcpus
  and is_current
  and price_hour is not null
  and accelerators = 0
  and category != 'Accelerator optimized'
  and instance not like '%metal%'
  and instance not like 'ct%'
  and instance not like 'tpu%';
create view gcp_family as
  select * from gcp join
  (select case when two.instance is null then one.instance
              when ((two.net_gbitps/two.price_hour)/(one.net_gbitps/one.price_hour) > 1.1) then two.instance
              else one.instance end as instance
  from (select * from (select *, row_number() over (partition by family order by vcpus desc) r from gcp) where r = 1) one
  left join (select * from (select *, row_number() over (partition by family order by vcpus desc) r from gcp) where r = 2) two
  using (family)) using (instance);
create view gcp_accel as
  select * from gcp_all
  where category = 'Accelerator optimized' and accelerator_model is not null;
create view gcp_shared as
  select * from gcp_all where vcpus_base != vcpus;
COMMENT ON COLUMN gcp_all.price_hour IS 'us-central1 (lowest-price tier) Linux on-demand price per hour in USD, all-in: CPU + RAM + bundled local SSD + attached GPUs (null when a component has no on-demand SKU in the region)';
COMMENT ON COLUMN gcp_all.processor_model IS 'Family CPU platform (coarser than an exact model; NULL / "variable" where a family spans platforms)';
COMMENT ON COLUMN gcp_all.vcpus_base IS 'Baseline vCPUs; shared-core machines run below vcpus';
COMMENT ON COLUMN gcp_all.ebs_iops IS 'Always NULL: GCP Hyperdisk is provisioned per-disk, no per-machine-type cap';
COMMENT ON COLUMN gcp_all.instance IS 'Full name of the machine type (e.g., c4a-standard-4)';
COMMENT ON COLUMN gcp_all.family IS 'Machine family: name minus the size token, keeping the shape/memory-ratio (e.g. c2-standard-30 -> c2-standard, z3-highmem-14-standardlssd -> z3-highmem-standardlssd)';
COMMENT ON COLUMN gcp_all.category IS 'Machine category (e.g. General purpose, Compute optimized, Accelerator optimized)';
COMMENT ON COLUMN gcp_all.ram_gib IS 'Amount of main memory in GiB';
COMMENT ON COLUMN gcp_all.vcpus IS 'Number of vCPUs (hyperthreads)';
COMMENT ON COLUMN gcp_all.cores IS 'Number of physical cores (vCPUs / threads-per-core; NULL for shared-core)';
COMMENT ON COLUMN gcp_all.arch IS 'Processor architecture';
COMMENT ON COLUMN gcp_all.net_gbitps IS 'Default egress network bandwidth in Gbit/s';
COMMENT ON COLUMN gcp_all.net_peak_gbitps IS 'Peak (Tier_1) egress network bandwidth in Gbit/s';
COMMENT ON COLUMN gcp_all.storage_gb IS 'Bundled local SSD storage in GB (NULL when none); physical disk is 375 GiB except Z3/bare-metal which use 3-6 TiB disks';
COMMENT ON COLUMN gcp_all.storage_count IS 'Number of physical bundled local SSD disks';
COMMENT ON COLUMN gcp_all.storage_is_ssd IS 'Whether bundled storage is SSD';
COMMENT ON COLUMN gcp_all.storage_is_nvme IS 'Whether bundled local SSD uses the NVMe interface';
COMMENT ON COLUMN gcp_all.ebs_gbitps IS 'Always NULL (see ebs_iops)';
COMMENT ON COLUMN gcp_all.ebs_peak_iops IS 'Always NULL (see ebs_iops)';
COMMENT ON COLUMN gcp_all.ebs_peak_gbitps IS 'Always NULL (see ebs_iops)';
COMMENT ON COLUMN gcp_all.accelerators IS 'Number of attached accelerators (GPUs/TPU chips)';
COMMENT ON COLUMN gcp_all.accelerator_model IS 'Accelerator model (display name; memory variants like A100 40/80GB share a name -- see accelerator_gib)';
COMMENT ON COLUMN gcp_all.accelerator_gib IS 'Total accelerator memory (nominal GB, curated from GCP docs; NULL when unknown)';
COMMENT ON COLUMN gcp_all.is_current IS 'Whether this machine type is current (not deprecated)';
COMMENT ON COLUMN gcp_all.storage_read_iops IS 'Bundled local SSD random read IOPS';
COMMENT ON COLUMN gcp_all.storage_write_iops IS 'Bundled local SSD random write IOPS';
COMMENT ON COLUMN gcp_all.release_year IS 'Family GA date as a fractional year, year + (month-1)/12';
"""


def write_duckdb(rows, out_path):
    con = duckdb.connect(out_path)
    cols_ddl = ", ".join(f'"{n}" {t}' for n, t in COLUMNS)
    # Explicitly drop views before the base table (CASCADE is unreliable across reruns).
    for v in ("gcp_family", "gcp_accel", "gcp_shared", "gcp"):
        con.execute(f"drop view if exists {v}")
    con.execute("drop table if exists gcp")   # legacy: gcp was once a table
    con.execute("drop table if exists gcp_all cascade")
    con.execute(f"create table gcp_all ({cols_ddl})")
    con.executemany(
        f"insert into gcp_all values ({', '.join(['?'] * len(COLUMNS))})", rows
    )
    con.execute(VIEWS_SQL)
    counts = {
        v: con.execute(f"select count(*) from {v}").fetchone()[0]
        for v in ("gcp_all", "gcp", "gcp_family", "gcp_accel", "gcp_shared")
    }
    con.close()
    print("Wrote " + out_path)
    for v, c in counts.items():
        print(f"  {v}: {c}")


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--output", default=os.path.join(HERE, "cloudspecs.duckdb"),
                    help="DuckDB file to write the gcp_all table + views into")
    ap.add_argument("--region", default=DEFAULT_REGION,
                    help="single region to price (default us-central1, a lowest-price tier)")
    ap.add_argument("--credentials", default=DEFAULT_CREDS)
    args = ap.parse_args()

    with open(args.credentials) as f:
        creds = json.load(f)
    print(f"Authenticating as {creds['client_email']} ...")
    token = get_access_token(creds)

    specs = fetch_machine_specs(token, creds["project_id"], args.region)
    net = fetch_network()
    core_rates, ram_rates, ssd_rates, gpu_rates = fetch_rates(token, args.region)

    print("Building rows ...")
    rows = build_rows(specs, net, core_rates, ram_rates, ssd_rates, gpu_rates, args.region)
    write_duckdb(rows, args.output)


if __name__ == "__main__":
    sys.exit(main())
