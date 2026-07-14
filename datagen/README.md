# cloudspecs builders (AWS + GCP + Azure + STACKIT + OVHcloud + Oracle + Hetzner)

Self-contained rewrite of the cloud scrapers that produce `cloudspecs.duckdb`.
No dependency on the rest of this repository. Only **Linux on-demand prices** are
collected (AWS: us-east-1; GCP: us-central1; Azure: eastus; STACKIT: eu01; OVHcloud:
standard region; Oracle: uniform commercial list price; Hetzner: fsn1 — each a low-price
reference).

- `build.py` — AWS EC2 → `aws_all` table + views
- `gcp.py` — GCP Compute Engine → `gcp_all` table + views
- `azure.py` — Azure Virtual Machines → `azure_all` table + views
- `stackit.py` — STACKIT Compute Engine → `stackit_all` table + views
- `ovh.py` — OVHcloud Public Cloud instances → `ovh_all` table + views
- `oracle.py` — Oracle Cloud (OCI) Compute shapes → `oracle_all` table + views
- `hetzner.py` — Hetzner Cloud servers → `hetzner_all` table + views

All seven write into the same `cloudspecs.duckdb` with an **identical 27-column
schema** (same names, same order), so the clouds are directly comparable (e.g.
`select * from aws_all union all select * from gcp_all union all select * from azure_all
union all select * from stackit_all union all select * from ovh_all union all select *
from oracle_all union all select * from hetzner_all`). Prices are USD (STACKIT's,
OVHcloud's and Hetzner's EUR prices are converted at the ECB reference rate).

## AWS — `build.py`

Gathers EC2 instance data from robust sources and writes one table (`aws_all`,
plus `benchmark`) and four views (`aws`, `aws_family`, `aws_accel`, `aws_burst`)
— identical schema to the old `cloudspecs/toduckdb.sh` output.

### Data sources

| Data | Source | Auth |
|------|--------|------|
| CPU/RAM/network/storage/EBS/accelerator specs | EC2 `DescribeInstanceTypes` (boto3) | AWS creds (describe-only) |
| Price, category, processor model, generation | Public [AWS bulk price list](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/us-east-1/index.json) for us-east-1 | none |
| Release dates | `https://instancetyp.es/timeline.json` | none |
| Instance-store read/write IOPS | AWS docs "instance store specifications" pages | none |
| SPEC benchmarks | `benchmark.csv` (static, shipped here) | — |
| Burstable (T2/T3/T3a/T4g) baseline | static table in `build.py` | — |

The instance **universe** is every type with a us-east-1 Linux on-demand price.
A handful of retired previous-generation types that AWS still prices but no
longer describes are backfilled from a frozen snapshot (`RETIRED_SPECS`).

### Usage

```sh
pip install -r requirements.txt

# credentials only need ec2:DescribeInstanceTypes
export AWS_SHARED_CREDENTIALS_FILE=/path/to/credentials   # or use ~/.aws
python build.py                      # -> cloudspecs.duckdb
python build.py --refresh            # re-download cached inputs (price list etc.)
```

Downloaded inputs are cached under `work/` (gitignored); the price list is
~480 MB, so the first run downloads it once and reuses it afterwards.

## GCP — `gcp.py`

The Compute Engine counterpart to `build.py`, same style and schema. Writes a
`gcp_all` table plus three views:

| Object | Contents |
|--------|----------|
| `gcp_all` | every machine type — all 27 `aws_all` columns |
| `gcp` | comparable slice: current, priced, non-shared-core, non-accelerator, non-metal, non-TPU (drops the same "strange instances" the AWS `aws` view does) |
| `gcp_accel` | accelerator (GPU) machine types |
| `gcp_burst` | shared-core machine types (`vcpus_base < vcpus`) |

GCP has **no per-machine price**: the hourly on-demand price is assembled as
`core_rate × vCPU + ram_rate × RAM_GiB` from per-family Core/Ram SKU rates.
Accelerator families are priced CPU+RAM only (the GPU SKU is separate); those
rows live in `gcp_accel`. `ebs_*` is always NULL — GCP's Hyperdisk families
provision disk performance per-disk with no published per-machine-type cap.

### Data sources

| Data | Source | Auth |
|------|--------|------|
| vCPU/RAM/arch/shared-cpu/GPU/local-SSD/deprecation | Compute Engine API `aggregated/machineTypes` | service account (compute.readonly) |
| Per-family Core/Ram $/hour rates | Cloud Billing Catalog API `services/<compute>/skus` | service account (cloud-billing.readonly) |
| Per-machine-type network bandwidth (default + Tier_1 egress) | GCP `cloud.google.com/compute/docs/*-machines` pages | none |
| Physical cores, IOPS, `vcpus_base`, `processor_model`, release year | derived / static tables in `gcp.py` | — |

### Setup & usage

Requires a service-account JSON key with **both** APIs enabled on the project
and the account granted Viewer (or `compute.readonly` + billing viewer):

- <https://console.cloud.google.com/apis/library/compute.googleapis.com>
- <https://console.cloud.google.com/apis/library/cloudbilling.googleapis.com>

```sh
pip install -r requirements.txt

# key defaults to ../gcp.json, or set GOOGLE_APPLICATION_CREDENTIALS
python gcp.py                        # -> cloudspecs.duckdb (adds gcp_all + views)
python gcp.py --region europe-west1  # price a different region
```

Run `build.py` **first** — it recreates `cloudspecs.duckdb` from scratch — then
`gcp.py`, `azure.py`, `stackit.py`, `ovh.py`, `oracle.py` and `hetzner.py`, which open the
existing file and only add/replace their own `gcp_*` / `azure_*` / `stackit_*` / `ovh_*` /
`oracle_*` / `hetzner_*` tables and views (in any order).

## Azure — `azure.py`

The Azure Virtual Machines counterpart to `build.py`, same style and schema.
Writes an `azure_all` table plus four views:

| Object | Contents |
|--------|----------|
| `azure_all` | every VM size — all 27 `aws_all` columns |
| `azure` | comparable slice: current, priced, full-vCPU (non-constrained), non-burstable (B-series), non-accelerator, non-HPC (drops the same "strange instances" the AWS `aws` view does) |
| `azure_family` | one representative size per family |
| `azure_accel` | GPU/accelerator (N-series) sizes, with the GPU model |
| `azure_burst` | burstable B-series sizes |

Unlike GCP, Azure has a **direct per-VM on-demand price** (Retail Prices API) and
rich API specs — physical cores (`vCPUsPerCore`), remote-disk throughput
(`UncachedDisk*` → `ebs_*`), local NVMe size + IOPS, and constrained-core sizes
(`vCPUsAvailable` → `vcpus_base`). Two things need non-API sources: network
bandwidth is scraped from the size docs, and `release_year` / `processor_model`
come from static tables in `azure.py`.

### Data sources

| Data | Source | Auth |
|------|--------|------|
| vCPU/RAM/cores/arch/GPU/local-disk/remote-disk throughput | Resource SKUs API `Microsoft.Compute/skus` | service principal (Reader) |
| Linux on-demand price | Public [Retail Prices API](https://prices.azure.com/api/retail/prices) (de-duped vs Windows/Spot/Low-Priority/Cloud-Services) | none |
| Max network bandwidth | Azure size docs `learn.microsoft.com/.../virtual-machines/sizes/*` (all series pages, cached) | none |
| Family, category, `vcpus_base`, GPU model, `processor_model`, GA year | derived / static tables in `azure.py` | — |

`net_gbitps` is Azure's single "Max Network Bandwidth" (no baseline/peak split),
NULL for the confidential DC-series and a few legacy sizes whose docs omit it.
`processor_model` is a coarse CPU vendor/generation, not an exact model.

### Setup & usage

Requires a service-principal JSON at `../azure.json` with keys `tenant_id`,
`client_id`, `client_secret`, `subscription_id`. The principal only needs
**Reader** on any one subscription (SKU metadata is subscription-independent):

```sh
az ad sp create-for-rbac --name cloudspecs-reader \
   --role Reader --scopes /subscriptions/<SUBSCRIPTION_ID>
```

```sh
pip install -r requirements.txt

# creds default to ../azure.json, or set AZURE_CREDENTIALS
python azure.py                      # -> cloudspecs.duckdb (adds azure_all + views)
python azure.py --region westus2     # price a different region
python azure.py --refresh            # re-download cached size doc pages
```

Size doc pages are cached under `work/azure/` (gitignored); `--refresh`
re-downloads them.

## STACKIT — `stackit.py`

The STACKIT (Schwarz Group) Compute Engine counterpart to `build.py`, same style and
schema. Writes a `stackit_all` table plus four views:

| Object | Contents |
|--------|----------|
| `stackit_all` | every flavor — all 27 `aws_all` columns |
| `stackit` | comparable slice: current, priced, non-GPU, non-burstable (drops the same "strange instances" the AWS `aws` view does) |
| `stackit_family` | one representative flavor per family |
| `stackit_accel` | GPU (n-series) flavors, with the GPU model |
| `stackit_burst` | CPU-overprovisioned ("burstable") families |

STACKIT is the simplest cloud here: **both data sources are public, no auth.** The
flavor universe and core specs come straight from the price list; the docs page only
enriches it. Prices are published in **EUR** and converted to **USD** (to match the
other clouds) at the live ECB reference rate.

### Data sources

| Data | Source | Auth |
|------|--------|------|
| Flavor universe, price, vCPU, RAM, CPU vendor, overprovisioning flag | Public [price-list JSON](https://pim.api.stackit.cloud/v1/skus?region=eu01) (`region=eu01`, standard/non-metro flavors) | none |
| Local disk, GPU model + memory, CPU microarchitecture | [Machine-types docs](https://docs.stackit.cloud/products/compute-engine/server/basics/machine-types/) | none |
| EUR→USD rate | Public [ECB daily reference rate](https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml) | none |
| Family, category, cores, release year, accelerator count | derived in `stackit.py` | — |

`net_gbitps` and `ebs_*` are always NULL — STACKIT publishes no per-flavor network or
block-storage throughput. `processor_model` is the CPU microarchitecture (coarse vendor
where the docs omit a family). `release_year` is the CPU generation's launch year (a
proxy; STACKIT publishes no GA date). Metro (distributed-placement) meters are dropped
as duplicates of the standard flavor at a higher price.

### Usage

```sh
pip install -r requirements.txt

python stackit.py                    # -> cloudspecs.duckdb (adds stackit_all + views)
python stackit.py --region eu02      # price a different region
python stackit.py --eur-usd 1.10     # pin the FX rate instead of the live ECB rate
python stackit.py --refresh          # re-download the cached price list + docs page
```

The price list and docs page are cached under `work/stackit/` (gitignored); `--refresh`
re-downloads them.

## OVHcloud — `ovh.py`

The OVHcloud Public Cloud counterpart to `build.py`, same style and schema. Writes an
`ovh_all` table plus four views:

| Object | Contents |
|--------|----------|
| `ovh_all` | every flavor — all 27 `aws_all` columns |
| `ovh` | comparable slice: current, priced, dedicated-resources (non-shared, non-sandbox), non-GPU |
| `ovh_family` | one representative flavor per family (net-efficiency window like `aws_family`) |
| `ovh_accel` | GPU flavors, with the GPU model |
| `ovh_burst` | sandbox / shared-vCore flavors, incl. the Discovery (d2) range — OVH's cheap tier |

Like STACKIT, **one public source, no auth** — and it's unusually complete. Every
instance flavor is an entry in the OVHcloud order catalog whose `blobs.technical` carries
the full spec (vCores, memory, public-network bandwidth, local disks + IOPS, GPU
model/memory/count) *and* whose `pricings` carries the hourly price. Prices are **EUR**,
converted to **USD** at the live ECB reference rate.

### Data sources

| Data | Source | Auth |
|------|--------|------|
| Flavor universe, price, vCPU, RAM, bandwidth, local disk, GPU | Public [order catalog](https://api.ovh.com/1.0/order/catalog/public/cloud?ovhSubsidiary=FR) (`publiccloud-instance` addons, base `<flavor>.consumption` plan, Linux) | none |
| EUR→USD rate | Public [ECB daily reference rate](https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml) | none |
| Family, category, CPU model, release year | derived / curated in `ovh.py` | — |

`cores` (physical) is inferred from the CPU's SMT: OVH publishes vCores, and on its
x86_64 hosts a vCore is one hardware thread of a 2-way-hyperthreaded core, so
`cores = vcpus/2` (ceil). Bare-metal flavors are the exception — the catalog reports their
real physical cores and threads directly (e.g. 16C/32T), so `vcpus = threads` and
`cores = cores`. `ebs_*` is
NULL (block-storage throughput is per-volume, not per-flavor). `processor_model` is
populated only for bare-metal flavors (which name a real CPU) and a curated per-family
table; standard flavors report a generic "vCore". `release_year` is an approximate
curated per-family GA year. The `win-*` (Windows) and spec-less catalog stubs are
skipped; flavors tagged `legacy` are kept in `ovh_all` but marked `is_current = false`.

### Usage

```sh
pip install -r requirements.txt

python ovh.py                        # -> cloudspecs.duckdb (adds ovh_all + views)
python ovh.py --subsidiary DE        # a different EUR catalog (price is uniform across EUR subs)
python ovh.py --eur-usd 1.10         # pin the FX rate instead of the live ECB rate
python ovh.py --refresh              # re-download the cached catalog
```

The catalog is cached under `work/ovh/` (gitignored); `--refresh` re-downloads it.

## Oracle — `oracle.py`

The Oracle Cloud Infrastructure (OCI) Compute counterpart to `build.py`, same style and
schema. Writes an `oracle_all` table plus four views:

| Object | Contents |
|--------|----------|
| `oracle_all` | every shape — all 27 `aws_all` columns |
| `oracle` | comparable slice: current, priced, non-GPU, non-bare-metal, non-micro |
| `oracle_family` | one representative shape per family (net-efficiency window like `aws_family`) |
| `oracle_accel` | GPU shapes, with the GPU model |
| `oracle_burst` | the Always-Free micro shape — OCI has no burstable-CPU family, so `vcpus_base` always equals `vcpus` |

Like GCP, OCI has **no per-shape price**: compute is billed per *OCPU-hour* plus per
*memory-GB-hour* (plus, for GPU shapes, per *GPU-hour*) at a rate that depends only on the
shape family (E4, E5, A1, …). Each family's OCPU / Memory / GPU rate is a catalog SKU
identified by a stable part number; `oracle.py` looks those up live (already in USD) and
assembles `price_hour = ocpu_rate·OCPUs + mem_rate·RAM_GiB (+ gpu_rate·nGPU)`. OCI's PAYG
list prices are uniform across commercial regions, so there is no region flag.

An **OCPU** is a billing unit, not a vCPU: on x86 (AMD/Intel) 1 OCPU = 1 core = 2 vCPUs;
on Ampere Altra (A1) 1 OCPU = 1 core = 1 vCPU; on AmpereOne (A2/A4) 1 OCPU = 2 cores =
2 vCPUs. Modern OCI shapes are **flexible** (you pick the OCPU count and memory), so each
flexible shape is enumerated over a grid: a doubling sequence of OCPU sizes up to the
family maximum, crossed with four memory points — 0.25×, 0.5×, 1× and 2× the family's
default GB/OCPU ratio (16 GB/OCPU for x86 and AmpereOne, 6 GB/OCPU for Altra). On x86 that
spans **2 / 4 / 8 / 16 GB per vCPU** — a compute point matching AWS c-series (2 GB/vCPU), a
general point, OCI's console default, and a memory point — so the RAM-per-vCPU axis stays
diverse and comparable to the other clouds rather than pinned to one ratio. OCI prices flex
shapes strictly linearly (per-OCPU + per-GB) with **no premium for a custom ratio** (unlike
GCP, where non-default configs cost ~10% more), so every point is priced exactly. Each row
is named `<shape>.<OCPUs>-<GB>` (e.g. `VM.Standard.E5.Flex.8-128`); points that collide at a
family's memory cap are de-duplicated. Fixed shapes (previous-generation VM.Standard2 / E2,
bare metal, GPU, DenseIO) are taken as-is.

### Data sources

| Data | Source | Auth |
|------|--------|------|
| Per-family OCPU-hour / memory-GB-hour / GPU-hour rates | Public [OCI cost-estimator catalog](https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/?currencyCode=USD) (by part number, already USD) | none |
| Shape specs (OCPU range, memory ratio, CPU, network, local NVMe, GPU) | [Compute Shapes docs](https://docs.oracle.com/en-us/iaas/Content/Compute/References/computeshapes.htm) — curated in `oracle.py` | none |
| Category, `vcpus`/`cores` mapping, release year | derived / curated in `oracle.py` | — |

`ebs_*` is always NULL — OCI block-volume performance is provisioned per-volume, not
per-shape. `net_gbitps` is the shape's max network bandwidth (scales with OCPU up to a
family cap); there is no separate baseline, so `net_peak == net`. `storage_*` is populated
only for DenseIO / GPU shapes with local NVMe. `processor_model` / `release_year` are the
family's CPU and GA year (curated; OCI publishes no per-shape GA date). Previous-generation
families (E2, VM.Standard2, E3, older GPUs) are kept in `oracle_all` but marked
`is_current = false`.

### Usage

```sh
pip install -r requirements.txt

python oracle.py                     # -> cloudspecs.duckdb (adds oracle_all + views)
python oracle.py --refresh           # re-download the cached price catalog
```

The price catalog is cached under `work/oracle/` (gitignored); `--refresh` re-downloads it.

## Hetzner — `hetzner.py`

The Hetzner Cloud (VPS) counterpart to `build.py`, same style and schema. Writes a
`hetzner_all` table plus four views:

| Object | Contents |
|--------|----------|
| `hetzner_all` | every server type — all 27 `aws_all` columns |
| `hetzner` | comparable slice: current, priced, **dedicated-vCPU (CCX)** |
| `hetzner_family` | one representative type per family |
| `hetzner_accel` | GPU types — empty (Hetzner Cloud has no GPU servers) |
| `hetzner_burst` | the shared-vCPU lines (CX / CPX / CAX) — Hetzner's oversubscribed tier |

The data source is the Hetzner Cloud API `GET /v1/server_types`, which carries every
server type's spec (vCPU `cores`, `memory`, local-NVMe `disk`, `cpu_type` shared/dedicated,
`architecture`) and per-location hourly prices. Unlike STACKIT/OVHcloud this API needs a
token, so — like the Hetzner **shared** vCPU lines being oversubscribed — the comparable
`hetzner` view keeps only the **dedicated** CCX line (the shared CX/CPX/CAX lines go to
`hetzner_burst`, matching how STACKIT's overprovisioned and OVHcloud's shared-vCore
families are handled). Prices are the compute-only server price (the primary IPv4 is billed
separately) at the low-price EU reference location fsn1 (== nbg1 / hel1; ash/hil/sin cost
more), in EUR, converted to USD at the live ECB reference rate.

### Data sources

| Data | Source | Auth |
|------|--------|------|
| Server types (vCPU, RAM, disk, cpu_type, arch, price) | Hetzner Cloud API [`/v1/server_types`](https://docs.hetzner.cloud/) | Hetzner Cloud API token |
| EUR→USD rate | Public [ECB daily reference rate](https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml) | none |
| CPU model, release year | derived / curated in `hetzner.py` | — |

`net_gbitps` and `ebs_*` are always NULL — Hetzner publishes no per-type network bandwidth
(only a monthly-traffic allowance), and Volume throughput is per-volume, not per-type.
`cores` (physical) is inferred from the CPU model's SMT (Ampere Altra CAX = 1 thread/core so
`cores = vcpus`; Intel Xeon / AMD EPYC = 2-way SMT so `cores = vcpus/2`), since Hetzner
publishes only vCPUs. `storage_gb` is the included local NVMe SSD. `accelerators` is always 0.
`processor_model` / `release_year` are curated per family (the API gives neither); CX gen3
deliberately runs on mixed recycled Intel Xeon Gold / AMD silicon.

### Setup & usage

The API needs a (read-only) Hetzner Cloud API token — create one in any Hetzner Cloud
project (Security → API tokens). Set `HCLOUD_TOKEN` or drop the token in `../hetzner.txt`:

```sh
pip install -r requirements.txt

export HCLOUD_TOKEN=...               # or put it in ../hetzner.txt
python hetzner.py --refresh           # -> cloudspecs.duckdb (adds hetzner_all + views)
python hetzner.py --location nbg1      # price a different location
python hetzner.py --eur-usd 1.10       # pin the FX rate instead of the live ECB rate
```

Without a token `hetzner.py` builds from a **frozen snapshot** of the current EU types
baked into the script (so the DB is always produced); `--refresh` with a token re-fetches
live (cached under `work/hetzner/`, gitignored) and additionally picks up any
deprecated-but-still-priced types (flagged `is_current = false`).

## Debugging

Compare against a previous database — a correct build differs only by newer
instances:

```sh
python compare.py /path/to/old/cloudspecs.duckdb cloudspecs.duckdb
```
