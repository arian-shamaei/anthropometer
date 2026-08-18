#!/bin/sh
# Copy the canonical python engine into the cargo package (rust/engine/) so
# `cargo publish` ships a self-contained crate. Run before every publish.
# Core = amtr_engine.py ONLY — the report modules ship separately as the
# `amtr-paper` pip package (see report/), which the engine discovers on PATH.
# (amtrino vendors its own copy: see arian-shamaei/amtrino scripts/sync-engine.sh)
set -e
cd "$(dirname "$0")/.."
cp amtr_engine.py rust/engine/
rm -f rust/engine/amtr_paper.py rust/engine/amtr_figures.py \
      rust/engine/amtr_phases.py rust/engine/amtr_turns.py
echo "synced $(ls rust/engine | wc -l | tr -d ' ') file(s) into rust/engine/"
