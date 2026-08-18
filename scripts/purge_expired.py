"""CLI: delete documents past the retention period (NFR-003d).

The period comes from `retention.document_days` in app.yaml. It is null by
default, meaning keep for ever, and then this script deletes nothing.

Purging is not automatic — run this from cron, a scheduled task, or by hand.
Nothing in the requirements says when it should run, so nothing schedules it.

Usage:
    python scripts/purge_expired.py            # obey app.yaml, really delete
    python scripts/purge_expired.py --dry-run  # list what would go
    python scripts/purge_expired.py --days 30  # override the configured period
"""

# 1. Standard library imports
import argparse
import logging

# 2. Third-party imports
from dotenv import load_dotenv

# 3. Internal imports
from sentineliq import service
from sentineliq.components.database import repository as repo
from sentineliq.config import load_app_config

logger = logging.getLogger("purge_expired")


def main() -> None:
    """Purge every tenant's expired documents."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=int,
        help="Retention period in days, overriding app.yaml. 0 deletes everything.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would be deleted."
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_dotenv()

    days = (
        args.days
        if args.days is not None
        else load_app_config().retention.document_days
    )
    if days is None:
        logger.info("Retention is not configured (retention.document_days is null).")
        logger.info("Nothing was deleted. Set it in app.yaml or pass --days.")
        return

    engine = repo.build_engine()
    repo.create_all(engine)
    factory = repo.session_factory(engine)

    total = 0
    with repo.session_scope(factory) as session:
        for tenant_id in repo.list_tenant_ids(session):
            if args.dry_run:
                from datetime import UTC, datetime, timedelta

                cutoff = datetime.now(UTC) - timedelta(days=days)
                expired = repo.list_documents_older_than(session, tenant_id, cutoff)
                for document in expired:
                    logger.info(
                        "would delete %s (%s) from %s",
                        document.id,
                        document.document_name,
                        tenant_id,
                    )
                total += len(expired)
            else:
                deleted = service.purge_expired_documents(session, tenant_id, days)
                for document_id in deleted:
                    logger.info("deleted %s from %s", document_id, tenant_id)
                total += len(deleted)

        if args.dry_run:
            session.rollback()

    verb = "would be deleted" if args.dry_run else "deleted"
    logger.info("%d document(s) %s (retention %d days).", total, verb, days)


if __name__ == "__main__":
    main()
