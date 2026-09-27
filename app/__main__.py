import argparse
import logging
import sys

from app.core.config import get_settings
from app.core.logging import setup_logging

log = logging.getLogger("app")


def main() -> int:
    try:
        return _run()
    except Exception:
        log.exception("fatal error")
        return 1


def _run() -> int:
    parser = argparse.ArgumentParser(prog="python -m app")
    parser.add_argument("role", choices=["api", "scheduler", "worker", "migrate"])
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level)

    if args.role == "api":
        import uvicorn

        from app.api.main import create_app

        uvicorn.run(create_app(), host=settings.api_host, port=settings.api_port, log_config=None)
        return 0
    if args.role == "worker":
        from app.worker.loop import run_worker

        return run_worker(settings)
    if args.role == "scheduler":
        from app.scheduler.leader import run_scheduler

        return run_scheduler(settings)
    if args.role == "migrate":
        from app.core.migrate import upgrade_head

        upgrade_head()
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
