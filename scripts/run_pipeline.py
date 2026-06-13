from __future__ import annotations

import argparse
import sys
from pathlib import Path

from loguru import logger


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.run_step import setup_logging


TEXT_PIPELINE = ["ingest", "bible", "planner", "review"]
FULL_PIPELINE = TEXT_PIPELINE + ["tts", "audio"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--chapter")
    parser.add_argument("--include-tts", action="store_true")
    parser.add_argument("--from-step", choices=FULL_PIPELINE, default=TEXT_PIPELINE[0])
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    project_dir = args.project.resolve()
    setup_logging(project_dir, args.verbose)
    pipeline = FULL_PIPELINE if args.include_tts else TEXT_PIPELINE
    if args.from_step not in pipeline:
        raise SystemExit(f"step {args.from_step!r} is not enabled; pass --include-tts to run tts/audio")

    start = pipeline.index(args.from_step)
    for step in pipeline[start:]:
        logger.info("Running step: {}", step)
        module = __import__(f"novelreader_v2.{step}.main", fromlist=["run"])
        module.run(project_dir, args.config.resolve() if args.config else None, args.chapter)
    logger.info("Pipeline complete")


if __name__ == "__main__":
    main()
