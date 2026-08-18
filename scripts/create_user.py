"""CLI: create a user so someone can sign in to the API and dashboard.

Usage:
    python scripts/create_user.py alice --tenant tenant-a --role analyst
"""

# 1. Standard library imports
import argparse
import getpass
import logging

# 2. Third-party imports
from dotenv import load_dotenv

# 3. Internal imports
from sentineliq import service
from sentineliq.components.database import repository as repo

logger = logging.getLogger("create_user")


def main() -> None:
    """Parse arguments and create one user."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username")
    parser.add_argument("--tenant", required=True, help="Tenant this user belongs to")
    parser.add_argument("--role", default="analyst", choices=list(service.ROLES))
    parser.add_argument("--password", help="Prompted for if omitted")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_dotenv()

    password = args.password or getpass.getpass("Password: ")
    if not password:
        raise SystemExit("a password is required")

    engine = repo.build_engine()
    repo.create_all(engine)
    factory = repo.session_factory(engine)
    with repo.session_scope(factory) as session:
        if repo.get_user_by_username(session, args.username):
            raise SystemExit(f"user {args.username!r} already exists")
        user_id = service.register_user(
            session, args.tenant, args.username, password, args.role
        )

    logger.info(
        "Created %s (%s) in tenant %s — id %s",
        args.username,
        args.role,
        args.tenant,
        user_id,
    )


if __name__ == "__main__":
    main()
