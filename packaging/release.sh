#!/bin/sh
# release.sh VERSION "title" — the amtr shipping pipeline, end to end.
# Captured 2026-08-12 from the v0.3.0 release so every future release is one
# command. Requires: gh (authenticated), cargo logged in to crates.io (a
# one-time `cargo login` — the token persists in ~/.cargo/credentials.toml),
# and a clean working tree on main.
#
#   ./packaging/release.sh 0.4.0 "one-line release title"
#
# What it does, in order:
#   1. sync the Python engine into rust/engine/ (self-contained crate)
#   2. bump rust/Cargo.toml, rebuild (refreshes Cargo.lock), run all tests
#   3. commit + tag vVERSION + push — the tag fires
#      .github/workflows/release.yml (4-target binaries + install.sh)
#   4. cargo publish (crates.io + lib.rs discovery feeds — the measured
#      best star channel after awesome-list PRs)
#   5. wait for CI, then bump the homebrew formula (repo AND tap, lockstep)
#   6. print the `gh release edit` reminder for title + notes
set -e
V=${1:?usage: release.sh VERSION "title"}
TITLE=${2:?usage: release.sh VERSION "title"}
cd "$(dirname "$0")/.."

[ -z "$(git status --porcelain)" ] || { echo "abort: working tree not clean"; exit 1; }
[ "$(git branch --show-current)" = "main" ] || { echo "abort: not on main"; exit 1; }

./packaging/sync-engine.sh
sed -i '' "s/^version = \".*\"/version = \"$V\"/" rust/Cargo.toml
(cd rust && cargo build --release && cargo test)

git add rust/Cargo.toml rust/Cargo.lock
git commit -m "v$V: $TITLE, version bump"
git tag "v$V"
git push origin main "v$V"

(cd rust && cargo publish)

# wait for the binary build so the release page is complete before we link it
RUN=$(gh run list --workflow=release --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch "$RUN" --exit-status

# homebrew: source-tarball sha256 → formula in this repo and in the tap
SHA=$(curl -sL "https://github.com/arian-shamaei/anthropometer/archive/refs/tags/v$V.tar.gz" \
  | shasum -a 256 | cut -d' ' -f1)
sed -i '' \
  -e "s|tags/v[0-9.]*\.tar\.gz|tags/v$V.tar.gz|" \
  -e "s|sha256 \".*\"|sha256 \"$SHA\"|" packaging/homebrew/amtr.rb
git add packaging/homebrew/amtr.rb
git commit -m "packaging: homebrew formula -> v$V"
git push
T=$(mktemp -d)
gh repo clone arian-shamaei/homebrew-anthropometer "$T" -- -q
cp packaging/homebrew/amtr.rb "$T/Formula/amtr.rb"
(cd "$T" && git commit -am "amtr $V — $TITLE" && git push)
rm -rf "$T"

echo
echo "shipped v$V. Last step (notes are hand-written, see past releases):"
echo "  gh release edit v$V --title \"v$V — $TITLE\" --notes '...'"
echo "reminder: never brew install amtr on this machine — the dev binary owns the command."
