from __future__ import annotations

import argparse
import sys
from pathlib import Path

from loguru import logger


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


STEPS = {
    "ingest": "novelreader_v2.ingest.main",
    "bible": "novelreader_v2.bible.main",
    "graph_index": "novelreader_v2.graph_index.main",
    "context_retrieval": "novelreader_v2.context_retrieval.main",
    "planner": "novelreader_v2.planner.main",
    "review": "novelreader_v2.review.main",
    "tts": "novelreader_v2.tts.main",
    "audio": "novelreader_v2.audio.main",
}


def setup_logging(project_dir: Path, verbose: bool) -> None:
    logger.remove()
    logger.add(sys.stderr, level="DEBUG" if verbose else "INFO", colorize=True)
    log_dir = project_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(log_dir / "novelreader_v2.log", level="DEBUG", encoding="utf-8", rotation="5 MB")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("step", choices=STEPS)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--chapter")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    project_dir = args.project.resolve()
    setup_logging(project_dir, args.verbose)
    module = __import__(STEPS[args.step], fromlist=["run"])
    module.run(project_dir, args.config.resolve() if args.config else None, args.chapter)


if __name__ == "__main__":
    main()
