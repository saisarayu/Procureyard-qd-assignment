#!/usr/bin/env python3
"""Evaluation runner entry point per SUBMISSION.md: python evals/run.py <service_url>"""
import sys
import argparse
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.evaluation.runner import run_evaluation

def main():
    parser = argparse.ArgumentParser(description="QuickDrop Evaluation Runner")
    parser.add_argument("url", nargs="?", default="http://localhost:8000", help="Service URL")
    parser.add_argument("--payswift-url", default=None, help="PaySwift Sandbox URL (optional)")
    args = parser.parse_args()

    success = asyncio.run(run_evaluation(base_url=args.url, payswift_url=args.payswift_url))
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
