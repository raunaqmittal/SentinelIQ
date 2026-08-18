"""CLI: download source data for SentinelIQ.

Usage:
    python scripts/ingest.py edgar MSFT CRM
"""

import argparse
import logging

from sentineliq.components.ingestion.edgar import fetch_vendor

logger = logging.getLogger("ingest")


def main() -> None:
    """Parse arguments and run the requested acquisition."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", choices=["edgar"], help="Data source to download")
    parser.add_argument("tickers", nargs="+", help="Ticker symbols, e.g. MSFT CRM")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for ticker in args.tickers:
        filing, facts = fetch_vendor(ticker)
        logger.info("%s -> %s, %s", ticker.upper(), filing.name, facts.name)


if __name__ == "__main__":
    main()
