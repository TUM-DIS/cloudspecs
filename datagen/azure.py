#!/usr/bin/env python3
"""
Get Microsoft Azure Virtual Machine instance data.

Standalone Azure counterpart to build.py / gcp.py. Collects VM size specs and the
Linux on-demand price in one lowest-price reference region (eastus), and writes an
`azure_all` table + views into DuckDB. Columns match aws_all exactly, so the three
clouds (AWS, GCP, Azure) are directly comparable.

Output objects (mirroring the AWS/GCP builds):
  azure_all    every VM size, all 27 aws_all columns.
  azure        comparable slice: current, priced, full-vCPU (non-constrained),
               non-burstable (B-series), non-accelerator, non-HPC.
  azure_family one representative size per family (largest, or 2nd-largest if
               >1.1x more network-efficient per dollar -- degrades to "largest"
               since net_gbitps is null); mirrors aws_family.
  azure_accel  GPU/accelerator (N-series) sizes, with the GPU model.
  azure_shared  burstable B-series sizes.

Data sources:
  1. Resource SKUs API  (Microsoft.Compute/skus, ARM management plane) -> specs
       vCPUs, MemoryGB, vCPUsPerCore (-> physical cores), CpuArchitectureType,
       GPUs, local NVMe / temp disk, UncachedDisk* (remote-disk throughput cap),
       vCPUsAvailable (constrained-core sizes), RetirementDateUtc. Needs a service
       principal with Reader (see azure.json).
  2. Retail Prices API  (prices.azure.com, public, no auth) -> pricing
       the Linux on-demand ("Consumption") hourly price per size, de-duplicated
       against Windows / Spot / Low-Priority / Cloud-Services meters.
  3. Azure size doc pages (learn.microsoft.com/.../virtual-machines/sizes/*) ->
       per-size max network bandwidth (the only source; no API field exposes it).

Derived / static: family & category (from the size name), physical cores
(vCPUs / vCPUsPerCore), processor_model (coarse CPU vendor), release_year
(series-version heuristic), accelerator model (name-token map).

Auth: a service-principal JSON at ../azure.json with keys tenant_id, client_id,
client_secret, subscription_id (Reader on any one subscription -- SKU metadata is
subscription-independent). See azure-integration notes.

Notes vs AWS: net_gbitps / net_peak_gbitps come from the size docs (no API field),
so they carry Azure's single "Max Network Bandwidth" (no baseline/peak split) and are
null for series the docs omit (confidential DC*, some legacy). processor_model is a
coarse CPU vendor/generation, not an exact model. release_year is approximate
(a curated series-GA table). ebs_iops / ebs_gbitps are the
"max uncached data-disk" throughput cap (Azure's analog of EBS-optimized bandwidth),
with no baseline/peak split so peak == base.
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
from datetime import date, datetime

import duckdb
from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_CREDS = os.environ.get("AZURE_CREDENTIALS", os.path.join(HERE, "..", "azure.json"))
DEFAULT_REGION = "eastus"  # Azure's low-price US reference region; change with --region

AUTHORITY = "https://login.microsoftonline.com"
MGMT_API = "https://management.azure.com"
MGMT_SCOPE = "https://management.azure.com/.default"
SKUS_API_VERSION = "2021-07-01"
RETAIL_API = "https://prices.azure.com/api/retail/prices"
RETAIL_API_VERSION = "2023-01-01-preview"

# Per-size network bandwidth lives only in the size doc pages (no API field). We
# enumerate every series page from the docs TOC and cache each page under work/azure.
DOC_BASE = "https://learn.microsoft.com/en-us/azure/virtual-machines/"
DOC_TOC = DOC_BASE + "toc.json"
DOC_CACHE = os.path.join(HERE, "work", "azure")


# --------------------------------------------------------------------------- #
# Auth + HTTP
# --------------------------------------------------------------------------- #
def get_access_token(creds):
    """Client-credentials OAuth token for the ARM management plane."""
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "scope": MGMT_SCOPE,
    }).encode()
    url = f"{AUTHORITY}/{creds['tenant_id']}/oauth2/v2.0/token"
    resp = urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=30)
    return json.load(resp)["access_token"]


def get_json(url, token=None):
    headers = {"Authorization": "Bearer " + token} if token else {}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise SystemExit(f"HTTP {e.code} for {url}\n{detail[:500]}")


# --------------------------------------------------------------------------- #
# Specs (Resource SKUs API)
# --------------------------------------------------------------------------- #
def _cap(caps, name, default=None):
    return caps.get(name, default)


def _family(size):
    """AWS-like family: the size name minus its vCPU count (and any constrained-core
    "-N" suffix), so the shape / feature letters / version stay part of the family
    (D4s_v5 -> Ds_v5; E96-48ads_v5 -> Eads_v5; NC24ads_A100_v4 -> NCads_A100_v4;
    B12ms -> Bms). Matches AWS (c7g) / GCP (c2-standard) family semantics."""
    m = re.match(r"^([A-Za-z]+)(\d+)(?:-\d+)?(.*)$", size)
    if not m:
        return size
    return m.group(1) + m.group(3)


def _prefix(size):
    """Leading letters of a size name (D4s_v5 -> D, NC24ads -> NC, FX64 -> FX)."""
    m = re.match(r"^([A-Za-z]+?)\d", size)
    return m.group(1).upper() if m else size.upper()


def _category(size):
    """Human category from the size-name prefix, matching AWS/GCP category values."""
    p = _prefix(size)
    if p.startswith("N"):                 # NC/ND/NV/NP/NG
        return "GPU"
    if p.startswith("H"):                 # HB/HC/HX/H
        return "HPC"
    if p.startswith("L"):                 # Lsv3 etc.
        return "Storage optimized"
    if p.startswith("F"):                 # F/FX
        return "Compute optimized"
    if p[0] in ("E", "M", "G"):           # E/Ec/M/G/GS
        return "Memory optimized"
    return "General purpose"              # A/B/D/DC/DS


def _feat_ver(size):
    """(feature-letters, version-int) from a size, e.g. D4ads_v5 -> ('ads', 5)."""
    m = re.match(r"^[A-Za-z]+\d+(?:-\d+)?(.*)$", size)
    rest = m.group(1) if m else ""
    parts = [p for p in rest.split("_") if p]
    feats = parts[0] if parts and not re.fullmatch(r"v\d+", parts[0]) else ""
    ver = next((int(p[1:]) for p in parts if re.fullmatch(r"v\d+", p)), None)
    return feats, ver


def _processor(arch, feats, ver):
    """Coarse CPU platform (vendor/gen), Azure's analog of AWS processor_model.
    Not an exact SKU -- 'a' feature letter => AMD, 'p'/Arm => Ampere/Cobalt,
    else Intel Xeon."""
    if arch == "arm64":
        return "Azure Cobalt 100" if (ver or 0) >= 6 else "Ampere Altra"
    if "a" in feats:
        return "AMD EPYC"
    return "Intel Xeon"


# Curated GA year per Azure series (Azure exposes no GA date via API). Keyed by
# "<PREFIX>_v<N>" (PREFIX = size letters before the vCPU count; "_v0" for the
# original, version-less series). Fractional year = year + (month-1)/12, month
# precision where a GA announcement is documented, else a bare year. Researched from
# Azure Updates / Compute & HPC blogs / vendor press, corroborated against Retail
# Prices effectiveStartDate. Comments flag the entries with real uncertainty.
_GA_YEAR = {
    "A_v2":  2016.83,  # ~Nov 2016 (uncertain; secondary sources)
    "B_v0":  2017.92,  # Dec 2017 (original Bs burstable)
    "B_v2":  2023.67,  # Sep 2023 (Bsv2/Basv2)
    "D_v0":  2014.67,  # Sep 2014 (original D-series)
    "D_v2":  2015.67,  # Sep 2015 (AzureCon; rollout Oct 2015)
    "D_v3":  2017.5,   # Jul 2017 (Dv3/Ev3)
    "D_v4":  2020.42,  # Jun 2020
    "D_v5":  2021.83,  # Nov 2021 (Ignite)
    "D_v6":  2025.08,  # Feb 2025 (Intel Emerald Rapids)
    "D_v7":  2026.0,   # Jan 2026 (AMD Turin)
    "DC_v3": 2022.33,  # May 2022 (Intel SGX)
    "DC_v5": 2022.5,   # Jul 2022 (AMD SEV-SNP)
    "DC_v6": 2025.67,  # Sep 2025 (AMD Genoa)
    "DS_v0": 2015.25,  # Apr 2015 (original DS, with Premium Storage)
    "DS_v2": 2015.75,  # Oct 2015 (uncertain month)
    "E_v3":  2017.5,   # Jul 2017 (first E-series)
    "E_v4":  2020.17,  # Mar 2020 (AMD Easv4)
    "E_v5":  2021.83,  # Nov 2021 (Ignite)
    "E_v6":  2025.08,  # Feb 2025 (E128 size specifically ~Aug 2025)
    "E_v7":  2026.0,   # Jan 2026 (AMD Turin)
    "EC_v5": 2022.5,   # Jul 2022 (ECasv5, with DCasv5)
    "EC_v6": 2025.67,  # Sep 2025 (ECasv6, with DCasv6)
    "F_v0":  2016.42,  # Jun 2016 (original F, Haswell)
    "F_v2":  2017.75,  # Oct 2017 (Fsv2, Skylake)
    "F_v6":  2024.92,  # Dec 2024 (AMD Genoa)
    "F_v7":  2026.0,   # Jan 2026 (AMD Turin)
    "FX_v0": 2021.42,  # Jun 2021 (original FX, Cascade Lake)
    "FX_v2": 2025.33,  # May 2025 (Emerald Rapids)
    "G_v0":  2015.0,   # Jan 2015 (original G-series)
    "GS_v0": 2015.67,  # Sep 2015 (G + premium storage)
    "HB_v2": 2020.08,  # Feb 2020 (AMD EPYC Rome)
    "HB_v3": 2021.17,  # Mar 2021 (AMD EPYC Milan)
    "HB_v4": 2023.42,  # Jun 2023 (AMD Genoa-X)
    "HB_v5": 2025.83,  # Nov 2025 (AMD EPYC + HBM3)
    "HC_v0": 2019.42,  # Jun 2019 (uncertain; preview Sep 2018)
    "HX_v0": 2023.42,  # Jun 2023 (Genoa-X, with HBv4)
    "L_v0":  2017.17,  # Mar 2017 (original Ls)
    "L_v2":  2019.08,  # Feb 2019 (Lsv2, AMD EPYC)
    "L_v3":  2022.42,  # Jun 2022 (Lsv3/Lasv3)
    "L_v4":  2025.25,  # Apr 2025 (Laosv4; full family Jun 2025)
    "M_v0":  2017.92,  # Dec 2017 (original M, up to 4TB)
    "M_v2":  2019.33,  # May 2019 (6TB; 12TB sizes Oct 2019)
    "M_v3":  2023.83,  # Nov 2023 (Medium Memory; High Mem Sep 2024)
    "NC_v3": 2018.17,  # Mar 2018 (Tesla V100)
    "NC_v4": 2022.42,  # Jun 2022 (A100)
    "NC_v5": 2024.17,  # Mar 2024 (H100)
    "ND_v2": 2020.17,  # Mar 2020 (V100 NVLink)
    "ND_v4": 2021.42,  # Jun 2021 (A100)
    "ND_v5": 2023.58,  # Aug 2023 (H100)
    "ND_v6": 2025.17,  # Mar 2025 (GB200)
    "NP_v0": 2021.33,  # May 2021 (Alveo U250 FPGA)
    "NV_v2": 2018.67,  # Sep 2018 (Tesla M60; uncertain)
    "NV_v3": 2019.58,  # ~Aug 2019 (uncertain, from pricing meters)
    "NV_v4": 2020.17,  # ~Mar 2020 (AMD MI25; uncertain month)
    "NV_v5": 2022.5,   # Jul 2022 (NVIDIA A10)
    "PB_v0": 2019.0,   # ~mid-2019 (Project Brainwave FPGA; heavily uncertain)
}


def _series_key(size):
    """Series key '<PREFIX>_v<N>' for the GA-year lookup ('_v0' when versionless)."""
    m = re.search(r"_v(\d+)", size)
    return f"{_prefix(size)}_v{int(m.group(1)) if m else 0}"


def _release_year(size):
    return _GA_YEAR.get(_series_key(size))


# guestAccelerator model + per-GPU memory (GiB), resolved from the size name. The
# accelerator is embedded in the name (_A100_/_H100_/_T4_/_A10_) or implied by the
# N-series root+version. Best-effort (coarse), mirroring GCP's GPU handling.
def _accel(name):
    n = name
    if "_H200" in n:
        return "NVIDIA H200", 141
    if "_H100" in n:                                   # NC*=NVL 94GB, ND*=SXM 80GB
        return ("NVIDIA H100 NVL", 94) if n.startswith("Standard_NC") else ("NVIDIA H100 SXM", 80)
    if "_A100" in n:
        return "NVIDIA A100", 80
    if "_A10" in n:
        return "NVIDIA A10", 24
    if "_T4" in n:
        return "NVIDIA Tesla T4", 16
    if n.startswith("Standard_NC") and n.endswith("_v3"):
        return "NVIDIA Tesla V100", 16
    if n.startswith("Standard_ND") and n.endswith("_v4"):    # ND96asr_v4 = A100 40GB
        return "NVIDIA A100", 40
    if n.startswith("Standard_ND") and n.endswith("_v2"):    # ND40rs_v2 = V100 32GB
        return "NVIDIA Tesla V100", 32
    if n.startswith("Standard_NV") and (n.endswith("_v2") or n.endswith("_v3")):
        return "NVIDIA Tesla M60", 8
    if n.startswith("Standard_NV") and n.endswith("_v4"):    # NV*as_v4 = AMD MI25
        return "AMD Radeon Instinct MI25", 16
    if n.startswith("Standard_NP"):                          # Alveo U250 FPGA
        return "Xilinx Alveo U250", 64
    return None, None


# Partitioned-GPU sizes: the SKUs API reports GPUs=1 for every slice of the
# physical GPU, so override accelerator_gib with the slice's memory (full GPU
# only at NV32as_v4 / NV36ads_A10_v5). accelerators stays 1 (BIGINT can't hold
# 1/8); the fraction is recoverable as accelerator_gib / full-GPU memory.
_GPU_SLICE_GIB = {
    "NV4as_v4": 2.0,          # 1/8 MI25
    "NV8as_v4": 4.0,          # 1/4 MI25
    "NV16as_v4": 8.0,         # 1/2 MI25
    "NV6ads_A10_v5": 4.0,     # 1/6 A10
    "NV12ads_A10_v5": 8.0,    # 1/3 A10
    "NV18ads_A10_v5": 12.0,   # 1/2 A10
}


def _storage(caps):
    """(storage_gb, count, is_ssd, is_nvme, read_iops, write_iops) for local disk.
    Prefer the real local NVMe SSD (Nvme* capabilities); else the legacy temp disk
    (MaxResourceVolumeMB). None when the size has no local storage."""
    nvme = int(_cap(caps, "NvmeDiskSizeInMiB", 0) or 0)
    if nvme > 0:
        per = int(_cap(caps, "NvmeSizePerDiskInMiB", 0) or 0)
        count = max(1, nvme // per) if per else 1
        r = _cap(caps, "NvmeMaxReadIops")
        w = _cap(caps, "NvmeMaxWriteIops")
        return (round(nvme / 1024), count, True, True,
                int(r) if r else None, int(w) if w else None)
    temp = int(_cap(caps, "MaxResourceVolumeMB", 0) or 0)
    if temp > 0:
        # legacy temp disk: SSD-backed on the current fleet; IOPS not separable from
        # cache (CombinedTempDiskAndCached*), so leave null.
        return round(temp / 1024), 1, True, False, None, None
    return None, 0, None, None, None, None


def _is_current(size, caps):
    """Whether the size is current: not promotional and not past its retirement date.
    (Azure has no explicit current-generation flag; this is a coarse heuristic.)"""
    if size.endswith("Promo"):
        return False
    r = _cap(caps, "RetirementDateUtc")
    if r:
        try:
            if datetime.strptime(r, "%m/%d/%Y").date() < date.today():
                return False
        except ValueError:
            pass
    return True


def fetch_specs(token, subscription, region):
    """Return {size: specs} for every VM size available in `region`."""
    print(f"Fetching Resource SKUs (region {region}) ...")
    url = (f"{MGMT_API}/subscriptions/{subscription}/providers/Microsoft.Compute/skus"
           f"?api-version={SKUS_API_VERSION}&$filter="
           + urllib.parse.quote(f"location eq '{region}'"))
    specs = {}
    while url:
        body = get_json(url, token)
        for sku in body.get("value", []):
            if sku.get("resourceType") != "virtualMachines":
                continue
            name = sku["name"]                    # "Standard_D4s_v5"
            size = sku["size"]                    # "D4s_v5"
            caps = {c["name"]: c["value"] for c in sku.get("capabilities", [])}
            vcpus = int(_cap(caps, "vCPUs", 0) or 0)
            per_core = int(_cap(caps, "vCPUsPerCore", 1) or 1)
            arch = "arm64" if _cap(caps, "CpuArchitectureType") == "Arm64" else "x86_64"
            feats, ver = _feat_ver(size)
            gpus = int(_cap(caps, "GPUs", 0) or 0)
            acc_model, acc_gib = _accel(name) if gpus else (None, None)
            s_gb, s_cnt, s_ssd, s_nvme, s_r, s_w = _storage(caps)
            ebs_iops = int(_cap(caps, "UncachedDiskIOPS", 0) or 0) or None
            ebs_bps = int(_cap(caps, "UncachedDiskBytesPerSecond", 0) or 0)
            ebs_gbitps = round(ebs_bps * 8 / 1e9, 3) if ebs_bps else None
            specs[size] = {
                "instance": size,
                "family": _family(size),
                "category": _category(size),
                "ram_gib": float(_cap(caps, "MemoryGB", 0) or 0),
                "vcpus": vcpus,
                "vcpus_base": float(_cap(caps, "vCPUsAvailable", vcpus) or vcpus),
                "cores": max(1, vcpus // per_core) if vcpus else None,
                "processor_model": _processor(arch, feats, ver),
                "arch": arch,
                "storage_gb": s_gb,
                "storage_count": s_cnt,
                "storage_is_ssd": s_ssd,
                "storage_is_nvme": s_nvme,
                "storage_read_iops": s_r,
                "storage_write_iops": s_w,
                "ebs_iops": ebs_iops,
                "ebs_gbitps": ebs_gbitps,
                "accelerators": gpus,
                "accelerator_model": acc_model,
                "accelerator_gib": _GPU_SLICE_GIB.get(size)
                                   or ((gpus * acc_gib) if acc_gib else None),
                "is_current": _is_current(size, caps),
                "release_year": _release_year(size),
            }
        url = body.get("nextLink")
    print(f"  {len(specs)} VM sizes in {region}")
    return specs


# --------------------------------------------------------------------------- #
# Pricing (Retail Prices API, no auth)
# --------------------------------------------------------------------------- #
def fetch_prices(region):
    """Return {armSkuName: linux_on_demand_price_per_hour} for `region`.

    Keeps only Consumption / 1-Hour meters, dropping Windows, Spot, Low-Priority and
    legacy Cloud-Services variants (which duplicate a size at a different price)."""
    print(f"Fetching Retail Prices (Consumption VMs, {region}) ...")
    filt = (f"serviceName eq 'Virtual Machines' and armRegionName eq '{region}' "
            f"and priceType eq 'Consumption'")
    url = (RETAIL_API + "?$filter=" + urllib.parse.quote(filt)
           + f"&api-version={RETAIL_API_VERSION}")
    prices = {}
    n_items = 0
    while url:
        body = get_json(url)
        for p in body.get("Items", []):
            n_items += 1
            if p.get("unitOfMeasure") != "1 Hour" or p.get("type") != "Consumption":
                continue
            arm = p.get("armSkuName")
            price = p.get("retailPrice")
            if not arm or not price:              # skip empty / $0 meters
                continue
            prod = (p.get("productName") or "").lower()
            blob = f"{prod} {p.get('meterName', '')} {p.get('skuName', '')}".lower()
            if "spot" in blob or "low priority" in blob:
                continue
            if "windows" in prod or "cloudservices" in prod.replace(" ", ""):
                continue
            # a size can still have >1 clean meter; keep the cheapest (the plain VM one)
            if arm not in prices or price < prices[arm]:
                prices[arm] = price
        url = body.get("NextPageLink")
    print(f"  scanned {n_items} price items; {len(prices)} Linux on-demand sizes priced")
    return prices


# --------------------------------------------------------------------------- #
# Network bandwidth (size doc pages)
# --------------------------------------------------------------------------- #
def _doc_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode("utf-8", "replace")


def _cached_page(slug, refresh):
    """Fetch a doc page (cached under work/azure); slug e.g. sizes/general-purpose/dv5-series."""
    path = os.path.join(DOC_CACHE, slug.replace("/", "__") + ".html")
    if refresh and os.path.exists(path):
        os.remove(path)
    if os.path.exists(path):
        with open(path) as f:
            return f.read()
    os.makedirs(DOC_CACHE, exist_ok=True)
    html = _doc_get(DOC_BASE + slug)
    with open(path, "w") as f:
        f.write(html)
    time.sleep(0.15)  # be polite to the docs server
    return html


def _net_from_page(html):
    """Return {size: max_network_gbitps} from a series page's network table. The
    header wording varies by page: 'Max Network Bandwidth (Mbps)' / '(Mb/s)' and,
    on accelerator pages, 'Max Bandwidth (Mbps)' -- so match bandwidth + a Mb unit."""
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for t in soup.find_all("table"):
        rows = t.find_all("tr")
        if not rows:
            continue
        hdr = [c.get_text(" ", strip=True).lower() for c in rows[0].find_all(["th", "td"])]
        name_col = next((i for i, h in enumerate(hdr) if "size name" in h or h == "size"), None)
        bw_col = next((i for i, h in enumerate(hdr)
                       if "bandwidth" in h and ("mbps" in h or "mb/s" in h)), None)
        if name_col is None or bw_col is None:
            continue
        for r in rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in r.find_all(["th", "td"])]
            if max(name_col, bw_col) >= len(cells):
                continue
            name = cells[name_col].strip()
            m = re.search(r"[\d.]+", cells[bw_col].replace(",", ""))
            if name.startswith("Standard_") and m:
                out[name[len("Standard_"):]] = float(m.group()) / 1000.0  # Mbps -> Gbit/s
    return out


def fetch_network(refresh=False):
    """Return {size: max_network_gbitps} scraped from the Azure size doc pages.

    Azure publishes only a single "Max Network Bandwidth" per size (no baseline/peak
    split). Confidential (DC*) and some legacy series omit the column entirely -> null.
    Never hard-fails the build: a TOC or page error just yields fewer entries."""
    print("Scraping per-size network bandwidth from Azure size docs ...")
    try:
        toc = _doc_get(DOC_TOC)
    except Exception as e:
        print(f"  WARNING: could not fetch docs TOC ({e}); net_gbitps will be null")
        return {}
    slugs = sorted(set(re.findall(r'"href":\s*"(sizes/[a-z0-9-]+/[a-z0-9-]+-series)"', toc)))
    net = {}
    for slug in slugs:
        try:
            net.update(_net_from_page(_cached_page(slug, refresh)))
        except Exception as e:
            print(f"  WARNING: doc page {slug} failed ({e})")
    print(f"  network bandwidth for {len(net)} sizes (from {len(slugs)} doc pages)")
    return net


# --------------------------------------------------------------------------- #
# Assemble
# --------------------------------------------------------------------------- #
# Columns match aws_all exactly (same names, same order), so azure_all is directly
# comparable to aws_all. net_* are kept for parity but always null (Azure exposes no
# per-size network-bandwidth field via the API).
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


def build_rows(specs, prices, net, region):
    rows = []
    no_price = []
    for size in sorted(specs):
        s = specs[size]
        price = prices.get("Standard_" + size)
        if price is None:
            no_price.append(size)
        # Azure publishes one max network bandwidth per size (no baseline/peak split),
        # so net_gbitps == net_peak_gbitps -- like ebs_* above.
        net_gbitps = net.get(size)
        rows.append((
            s["instance"],
            s["family"],
            s["category"],
            price,
            s["ram_gib"],
            s["vcpus"],
            s["vcpus_base"],
            s["cores"],
            s["processor_model"],
            s["arch"],
            net_gbitps,         # net_gbitps      -- max network bandwidth (docs)
            net_gbitps,         # net_peak_gbitps -- Azure gives one max value
            s["storage_gb"],
            s["storage_count"],
            s["storage_is_ssd"],
            s["storage_is_nvme"],
            s["ebs_iops"],
            s["ebs_gbitps"],
            s["ebs_iops"],      # ebs_peak_iops   -- Azure gives one (max) value,
            s["ebs_gbitps"],    # ebs_peak_gbitps    no baseline/peak split
            s["accelerators"],
            s["accelerator_model"],
            s["accelerator_gib"],
            s["is_current"],
            s["storage_read_iops"],
            s["storage_write_iops"],
            s["release_year"],
        ))
    if no_price:
        print(f"  NOTE: {len(no_price)} sizes have no Linux on-demand price in "
              f"{region} (price null): {', '.join(no_price[:20])}"
              + (" ..." if len(no_price) > 20 else ""))
    priced = len(rows) - len(no_price)
    print(f"  {priced}/{len(rows)} sizes priced")
    n_net = sum(1 for r in rows if r[10] is not None)
    print(f"  network bandwidth matched for {n_net}/{len(rows)} sizes")
    return rows


# Base table `azure_all` (all VM sizes) + views, mirroring the AWS/GCP builds.
# `azure` is the comparable slice: current, priced, full-vCPU (drops constrained
# "-N" sizes via vcpus_base = vcpus), non-burstable (B-series), non-accelerator,
# non-HPC -- i.e. the same "strange instances" the AWS/GCP views drop.
VIEWS_SQL = """
create view azure as
  select instance, family, category, price_hour, release_year, ram_gib, vcpus, cores, processor_model, arch, net_gbitps, net_peak_gbitps, storage_gb, storage_count, storage_read_iops, storage_write_iops, storage_is_ssd, storage_is_nvme, ebs_iops, ebs_gbitps, ebs_peak_iops, ebs_peak_gbitps
  from azure_all
  where vcpus_base = vcpus
  and is_current
  and price_hour is not null
  and accelerators = 0
  and category not in ('GPU', 'HPC')
  and family not like 'B%';
create view azure_family as
  select * from azure join
  (select case when two.instance is null then one.instance
              when ((two.net_gbitps/two.price_hour)/(one.net_gbitps/one.price_hour) > 1.1) then two.instance
              else one.instance end as instance
  from (select * from (select *, row_number() over (partition by family order by vcpus desc) r from azure) where r = 1) one
  left join (select * from (select *, row_number() over (partition by family order by vcpus desc) r from azure) where r = 2) two
  using (family)) using (instance);
create view azure_accel as
  select * from azure_all
  where category = 'GPU' and accelerator_model is not null;
create view azure_shared as
  select * from azure_all where family like 'B%';
COMMENT ON COLUMN azure_all.instance IS 'VM size name (e.g., D4s_v5); the deployable name is Standard_<instance>';
COMMENT ON COLUMN azure_all.price_hour IS 'eastus Linux on-demand price per hour in USD (Retail Prices API)';
COMMENT ON COLUMN azure_all.family IS 'VM family: size name minus the vCPU count, keeping the shape/feature letters/version (e.g. D4s_v5 -> Ds_v5, E96-48ads_v5 -> Eads_v5)';
COMMENT ON COLUMN azure_all.category IS 'VM category derived from the size prefix (General purpose, Compute optimized, Memory optimized, Storage optimized, GPU, HPC)';
COMMENT ON COLUMN azure_all.ram_gib IS 'Amount of main memory in GiB';
COMMENT ON COLUMN azure_all.vcpus IS 'Number of vCPUs';
COMMENT ON COLUMN azure_all.vcpus_base IS 'Available vCPUs (constrained-core sizes run below vcpus)';
COMMENT ON COLUMN azure_all.cores IS 'Number of physical cores (vCPUs / vCPUsPerCore)';
COMMENT ON COLUMN azure_all.processor_model IS 'Coarse CPU platform/vendor (Intel Xeon / AMD EPYC / Ampere Altra / Azure Cobalt), not an exact model';
COMMENT ON COLUMN azure_all.arch IS 'Processor architecture';
COMMENT ON COLUMN azure_all.net_gbitps IS 'Max network bandwidth in Gbit/s (from the size docs; NULL where the docs omit it, e.g. confidential DC* / some legacy sizes)';
COMMENT ON COLUMN azure_all.net_peak_gbitps IS 'Same as net_gbitps (Azure publishes one max value, no baseline/peak split)';
COMMENT ON COLUMN azure_all.storage_gb IS 'Local ephemeral SSD in GiB: local NVMe disk if present, else the temp disk (NULL when none)';
COMMENT ON COLUMN azure_all.storage_count IS 'Number of local disks';
COMMENT ON COLUMN azure_all.storage_is_ssd IS 'Whether local storage is SSD';
COMMENT ON COLUMN azure_all.storage_is_nvme IS 'Whether local storage is a directly-attached NVMe disk';
COMMENT ON COLUMN azure_all.ebs_iops IS 'Max uncached remote data-disk IOPS (Azure analog of EBS-optimized IOPS)';
COMMENT ON COLUMN azure_all.ebs_gbitps IS 'Max uncached remote data-disk bandwidth in Gbit/s';
COMMENT ON COLUMN azure_all.ebs_peak_iops IS 'Same as ebs_iops (Azure gives one max value, no baseline/peak split)';
COMMENT ON COLUMN azure_all.ebs_peak_gbitps IS 'Same as ebs_gbitps (see ebs_peak_iops)';
COMMENT ON COLUMN azure_all.accelerators IS 'Number of attached accelerators (GPUs/FPGAs); partitioned-GPU slices (NVv4, NVadsA10v5) count as 1';
COMMENT ON COLUMN azure_all.accelerator_model IS 'Accelerator model (best-effort, from the size name; memory variants like A100 40/80GB share a name -- see accelerator_gib)';
COMMENT ON COLUMN azure_all.accelerator_gib IS 'Total accelerator memory (nominal GB, best-effort); the slice memory on partitioned-GPU sizes';
COMMENT ON COLUMN azure_all.is_current IS 'Whether the size is current (not promotional / not past its retirement date)';
COMMENT ON COLUMN azure_all.storage_read_iops IS 'Local NVMe disk random read IOPS (NULL for temp-disk-only sizes)';
COMMENT ON COLUMN azure_all.storage_write_iops IS 'Local NVMe disk random write IOPS (NULL for temp-disk-only sizes)';
COMMENT ON COLUMN azure_all.release_year IS 'Series GA date as a fractional year (curated per-series table, month precision where documented)';
"""


def write_duckdb(rows, out_path):
    con = duckdb.connect(out_path)
    cols_ddl = ", ".join(f'"{n}" {t}' for n, t in COLUMNS)
    # Explicitly drop views before the base table (CASCADE is unreliable across reruns).
    for v in ("azure_family", "azure_accel", "azure_shared", "azure"):
        con.execute(f"drop view if exists {v}")
    con.execute("drop table if exists azure_all cascade")
    con.execute(f"create table azure_all ({cols_ddl})")
    con.executemany(
        f"insert into azure_all values ({', '.join(['?'] * len(COLUMNS))})", rows
    )
    con.execute(VIEWS_SQL)
    counts = {
        v: con.execute(f"select count(*) from {v}").fetchone()[0]
        for v in ("azure_all", "azure", "azure_family", "azure_accel", "azure_shared")
    }
    con.close()
    print("Wrote " + out_path)
    for v, c in counts.items():
        print(f"  {v}: {c}")


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--output", default=os.path.join(HERE, "cloudspecs.duckdb"),
                    help="DuckDB file to write the azure_all table + views into")
    ap.add_argument("--region", default=DEFAULT_REGION,
                    help="single region to price (default eastus)")
    ap.add_argument("--credentials", default=DEFAULT_CREDS)
    ap.add_argument("--refresh", action="store_true",
                    help="re-download cached size doc pages (network bandwidth)")
    args = ap.parse_args()

    with open(args.credentials) as f:
        creds = json.load(f)
    print(f"Authenticating service principal {creds['client_id']} ...")
    token = get_access_token(creds)

    specs = fetch_specs(token, creds["subscription_id"], args.region)
    prices = fetch_prices(args.region)
    net = fetch_network(args.refresh)

    print("Building rows ...")
    rows = build_rows(specs, prices, net, args.region)
    write_duckdb(rows, args.output)


if __name__ == "__main__":
    sys.exit(main())
