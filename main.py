from __future__ import annotations

import logging

from ajap import config, ingest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ajap.main")


def main() -> None:
    db_path = config.DB_PATH
    logger.info("Starting ingest pass → %s", db_path)
    summary = ingest.run_ingest(db_path)
    print(
        f"\nIngest complete: "
        f"{summary['fetched']} fetched / "
        f"{summary['new']} new / "
        f"{summary['duplicates']} duplicates"
    )


if __name__ == "__main__":
    main()
