from __future__ import annotations

import sys

from halyk_agent.cli import main


if __name__ == "__main__":
    args = ["validate", *sys.argv[1:]]
    raise SystemExit(main(args))

