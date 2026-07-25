#!/usr/bin/env python3
"""The single EUR->USD conversion factor used by the EUR-priced cloud builds.

STACKIT, OVHcloud, Hetzner and Scaleway publish their prices in EUR; cloudspecs stores
USD, so those four builds convert with this one constant.

It is a fixed constant rather than a live ECB lookup on purpose: each script used to fetch
the rate when it ran, so a database assembled over several days mixed conversion rates
(1.1424 for OVHcloud, 1.1430 for Hetzner, ...) and a price comparison between two European
clouds silently carried an FX difference on top of the price difference. One constant
makes those comparisons pure, and makes a rebuild reproducible.

Bump it -- and rebuild the four EUR clouds together -- when refreshing the database.
"""

# ECB euro foreign-exchange reference rate (https://www.ecb.europa.eu/stats/eurofxref/),
# 2026-07-24.
EUR_USD = 1.1377
