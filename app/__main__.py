import argparse
import getpass
import logging
import sys

from app.core.config import get_settings
from app.core.logging import setup_logging

log = logging.getLogger("app")

ROLES = ("api", "scheduler", "worker", "migrate")


def main() -> int:
    try:
        return _run()
    except Exception:
        log.exception("fatal error")
        return 1


def _read_password() -> str:
    """Wachtwoord interactief of via stdin; nooit via argv of env (zichtbaar in ps/logs)."""
    if sys.stdin.isatty():
        first = getpass.getpass("Wachtwoord: ")
        if first != getpass.getpass("Nogmaals: "):
            raise SystemExit("wachtwoorden komen niet overeen")
        return first
    return sys.stdin.readline().rstrip("\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app")
    sub = parser.add_subparsers(dest="command", required=True)
    for role in ROLES:
        sub.add_parser(role)
    user = sub.add_parser("create-user", help="lokale gebruiker aanmaken")
    user.add_argument("username")
    user.add_argument(
        "--role", action="append", default=[], choices=["viewer", "operator", "admin"]
    )
    user.add_argument("--display-name")
    token = sub.add_parser("create-token", help="API-token voor een lokale gebruiker")
    token.add_argument("username")
    token.add_argument("--name", required=True)
    token.add_argument("--expires-days", type=int, default=90)
    return parser


def _run() -> int:
    args = _parser().parse_args()
    settings = get_settings()
    setup_logging(settings.log_level)

    from app.core.db import set_application_name

    if args.command in ROLES:
        set_application_name(f"{args.command}:{settings.worker_id}")

    if args.command == "api":
        import uvicorn

        from app.api.main import create_app

        uvicorn.run(
            create_app(),
            host=settings.api_host,
            port=settings.api_port,
            log_config=None,
            # X-Forwarded-For/-Proto alleen van vertrouwde proxies (fase 5a).
            proxy_headers=True,
            forwarded_allow_ips=settings.trusted_proxies,
        )
        return 0
    if args.command == "worker":
        from app.worker.loop import run_worker

        return run_worker(settings)
    if args.command == "scheduler":
        from app.scheduler.leader import run_scheduler

        return run_scheduler(settings)
    if args.command == "migrate":
        from app.core.migrate import upgrade_head

        upgrade_head()
        return 0

    from app.core.db import get_sessionmaker
    from app.services import api_tokens, audit, users

    cli = audit.Actor("cli")
    with get_sessionmaker()() as session:
        if args.command == "create-user":
            user = users.create_local(
                session,
                args.username,
                _read_password(),
                args.role,
                display_name=args.display_name,
                actor=cli,
            )
            log.info("user created", extra={"username": user.username, "roles": user.roles})
            return 0
        if args.command == "create-token":
            owner = users.get_local(session, args.username)
            if owner is None:
                raise SystemExit(f"onbekende lokale gebruiker: {args.username}")
            new = api_tokens.create(
                session, owner, args.name, expires_days=args.expires_days, actor=cli
            )
            # Bewust naar stdout en niet via logging: dit is de uitvoer van het commando.
            sys.stdout.write(new.raw + "\n")
            return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
