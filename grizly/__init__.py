"""grizly — company lead research: public registry -> company website -> Claude -> CSV.

Country-agnostic core, pluggable registry adapters (`grizly.sources`) and seller
profiles (`profiles/*.toml`) that hold everything specific to one company:
offers, thresholds, routing, scoring, output format. Standard library only.
"""

VERSION = "2.0.0"
