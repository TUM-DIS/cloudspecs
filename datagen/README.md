# cloudspecs builders (AWS + GCP)

Self-contained rewrite of the cloud scrapers that produce `cloudspecs.duckdb`.
No dependency on the rest of this repository. Only **Linux on-demand prices** are
collected (AWS: us-east-1; GCP: us-central1, a lowest-price-tier region).

- `build.py` — AWS EC2 → `aws_all` table + views
- `gcp.py` — GCP Compute Engine → `gcp_all` table + views

Both write into the same `cloudspecs.duckdb` with an **identical 27-column
schema** (same names, same order), so the two clouds are directly comparable
(e.g. `select * from aws_all union all select * from gcp_all`).

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
`gcp.py`, which opens the existing file and only adds/replaces its own
`gcp_*` tables and views.

## Debugging

Compare against a previous database — a correct build differs only by newer
instances:

```sh
python compare.py /path/to/old/cloudspecs.duckdb cloudspecs.duckdb
```
