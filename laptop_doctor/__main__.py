import sys


def cli_check() -> int:
    from .checks import STATUS_LABEL, Status, run_all
    from .config import Config

    results = run_all(Config.load())
    for r in sorted(results, key=lambda r: -r.status):
        print(f"[{STATUS_LABEL[r.status]:^9}] {r.title}: {r.summary}")
        if "-v" in sys.argv and r.details:
            print("    " + r.details.replace("\n", "\n    "))
    return 1 if any(r.status >= Status.WARN for r in results) else 0


if "--check" in sys.argv:
    sys.exit(cli_check())

from .ui import main  # noqa: E402

sys.exit(main())
