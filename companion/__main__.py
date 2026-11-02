import os
import sys

# --demo: synthetic data for screenshots and UI work; never connects to Schwab, refuses orders.
if "--demo" in sys.argv:
    os.environ["EDGE_TRADER_DEMO"] = "1"
# --private: for screen recordings; hides carried positions, their orders and the account value.
if "--private" in sys.argv:
    os.environ["EDGE_TRADER_PRIVATE"] = "1"


def parse_broker_arg(argv: list[str] | None = None) -> str:
    if argv is None:
        argv = sys.argv[1:]
    broker = os.environ.get("EDGE_TRADER_BROKER", "schwab")
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--broker" and i + 1 < len(argv):
            broker = argv[i + 1]
            i += 2
            continue
        elif arg.startswith("--broker="):
            broker = arg.split("=", 1)[1]
            i += 1
            continue
        i += 1
    cleaned = broker.lower().strip()
    return cleaned if cleaned else "schwab"


os.environ["EDGE_TRADER_BROKER"] = parse_broker_arg()

if __name__ == "__main__":
    from .app import main  # noqa: E402
    main()
