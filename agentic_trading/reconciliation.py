"""Read-only venue recovery for orders previously submitted by this application."""
import argparse
import json
import time

from .api.service import Service
from .env import load_env


def main(argv=None):
    load_env()
    parser = argparse.ArgumentParser(prog="ct-reconcile")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--watch", action="store_true", help="keep synchronizing every 15 seconds")
    args = parser.parse_args(argv)
    service = Service(args.config_dir)
    while True:
        print(json.dumps(service.reconcile_orders(), default=str))
        if not args.watch:
            return 0
        time.sleep(15)


if __name__ == "__main__":
    raise SystemExit(main())
