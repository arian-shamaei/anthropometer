#!/bin/sh
# build.sh — build the amtr-report wheel + sdist.
#
# Vendors the canonical module copies from the repo root into this directory
# (the same sync-before-publish pattern as packaging/sync-engine.sh), then
# runs `python3 -m build`. Output lands in report/dist/.
#
#   ./report/build.sh            # build
#   ./report/build.sh --publish  # build then upload to PyPI with twine
set -e
cd "$(dirname "$0")"

cp ../amtr_paper.py ../amtr_figures.py ../amtr_phases.py ../amtr_turns.py \
   ../amtr_engine.py .
echo "vendored 5 modules from repo root"

python3 -m build

if [ "${1:-}" = "--publish" ]; then
  python3 -m twine upload dist/*
fi
