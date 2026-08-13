#!/bin/sh
# Copy the canonical python engine into the cargo package (rust/engine/) so
# `cargo publish` ships a self-contained crate. Run before every publish.
# (amtrino vendors its own copy: see arian-shamaei/amtrino scripts/sync-engine.sh)
set -e
cd "$(dirname "$0")/.."
cp amtr_engine.py amtr_paper.py amtr_figures.py amtr_phases.py amtr_turns.py rust/engine/
echo "synced $(ls rust/engine | wc -l | tr -d ' ') files into rust/engine/"
