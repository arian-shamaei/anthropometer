#!/bin/sh
# Copy the canonical python engine into every self-contained package:
#   rust/engine/                      -> `cargo publish` ships it in the crate
#   menubar/Sources/AmtrBar/Resources -> amtrino bundles it (fleet feed only)
# Run before every publish / menubar build.
set -e
cd "$(dirname "$0")/.."
cp amtr_engine.py amtr_paper.py amtr_figures.py amtr_phases.py amtr_turns.py rust/engine/
echo "synced $(ls rust/engine | wc -l | tr -d ' ') files into rust/engine/"
mkdir -p menubar/Sources/AmtrBar/Resources
cp amtr_engine.py menubar/Sources/AmtrBar/Resources/
echo "synced amtr_engine.py into menubar/Sources/AmtrBar/Resources/"
