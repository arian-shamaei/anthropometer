#!/bin/sh
# install.sh — one-line installer for `amtr` (anthropometer).
#
#   curl -fsSL https://raw.githubusercontent.com/arian-shamaei/anthropometer/main/install.sh | sh
#
# Downloads a prebuilt `amtr` binary bundle (binary + stdlib Python engine) for
# your platform from the latest GitHub release and installs it under
# ~/.local (no Rust, no Homebrew). POSIX sh — no bashisms.
#
# Environment overrides:
#   AMTR_VERSION   install a specific tag (e.g. v0.1.0) instead of "latest"
#   AMTR_PREFIX    install prefix (default: $HOME/.local)
#   AMTR_TARBALL   use a local tarball instead of downloading (offline install)

set -eu

REPO="arian-shamaei/anthropometer"
PREFIX="${AMTR_PREFIX:-$HOME/.local}"
BUNDLEDIR="$PREFIX/lib/amtr"     # the whole bundle (binary + engine) lands here
BINDIR="$PREFIX/bin"             # a symlink to the binary goes here
#
# Why $PREFIX/lib/amtr and not libexec/amtr? ipc.rs::default_engine_path checks,
# in order: (1) amtr_engine.py NEXT TO the exe, (2) ../libexec/amtr_engine.py,
# (3) ../lib/amtr/amtr_engine.py. On LINUX, current_exe() resolves the bin/amtr
# symlink to the real path in the bundle dir, so (1) wins. On macOS, current_exe()
# does NOT resolve the symlink (it returns $PREFIX/bin/amtr), so (1) misses — but
# with the bundle in $PREFIX/lib/amtr, check (3) `$PREFIX/bin/../lib/amtr/...`
# resolves to the engine. So this one layout is correct on BOTH platforms; the
# libexec/amtr layout would leave macOS with no matching candidate.

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
err()  { printf 'amtr install: error: %s\n' "$1" >&2; exit 1; }
note() { printf '%s\n' "$1" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
# prerequisites
# ---------------------------------------------------------------------------
have tar || err "tar is required but was not found"
if [ -z "${AMTR_TARBALL:-}" ]; then
  have curl || err "curl is required but was not found"
fi

# ---------------------------------------------------------------------------
# detect OS + arch -> Rust target triple (must match release.yml's matrix)
# ---------------------------------------------------------------------------
os="$(uname -s)"
arch="$(uname -m)"
case "$os" in
  Darwin)
    case "$arch" in
      arm64 | aarch64) target="aarch64-apple-darwin" ;;
      x86_64)          target="x86_64-apple-darwin" ;;
      *) err "unsupported macOS architecture: $arch" ;;
    esac
    ;;
  Linux)
    case "$arch" in
      x86_64 | amd64)  target="x86_64-unknown-linux-gnu" ;;
      aarch64 | arm64) target="aarch64-unknown-linux-gnu" ;;
      *) err "unsupported Linux architecture: $arch" ;;
    esac
    ;;
  *)
    err "unsupported OS: $os (amtr ships prebuilt binaries for macOS and Linux)"
    ;;
esac

# ---------------------------------------------------------------------------
# resolve the release tag
# ---------------------------------------------------------------------------
if [ -n "${AMTR_VERSION:-}" ]; then
  tag="$AMTR_VERSION"
else
  note "Resolving latest amtr release..."
  api="https://api.github.com/repos/$REPO/releases/latest"
  # Pull "tag_name": "vX.Y.Z" out of the JSON without assuming jq is installed.
  # The tag carries its leading "v" and is used VERBATIM — release.yml names the
  # asset with the same ${GITHUB_REF_NAME}, so the two must not diverge.
  tag="$(curl -fsSL "$api" \
    | grep '"tag_name"' \
    | head -n1 \
    | sed -E 's/.*"tag_name"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')"
  [ -n "$tag" ] || err "could not determine the latest release tag (set AMTR_VERSION to override)"
fi

# Asset name — MUST equal release.yml's `amtr-${tag}-${target}.tar.gz`.
asset="amtr-${tag}-${target}.tar.gz"
url="https://github.com/$REPO/releases/download/${tag}/${asset}"

# ---------------------------------------------------------------------------
# temp workspace (always cleaned up)
# ---------------------------------------------------------------------------
tmp="$(mktemp -d "${TMPDIR:-/tmp}/amtr-install.XXXXXX")"
cleanup() { rm -rf "$tmp"; }
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# fetch the tarball (AMTR_TARBALL short-circuits the download)
# ---------------------------------------------------------------------------
if [ -n "${AMTR_TARBALL:-}" ]; then
  note "Using local tarball: $AMTR_TARBALL"
  [ -f "$AMTR_TARBALL" ] || err "AMTR_TARBALL not found: $AMTR_TARBALL"
  cp "$AMTR_TARBALL" "$tmp/$asset"
else
  note "Downloading $url"
  curl -fsSL "$url" -o "$tmp/$asset" \
    || err "download failed: $url (check that release $tag has a $target build)"
fi

# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------
mkdir -p "$tmp/unpacked"
tar -xzf "$tmp/$asset" -C "$tmp/unpacked" || err "failed to extract $asset"

# The release tarball is flat (contents at the top level), but tolerate a single
# leading directory too, so we find the binary rather than assume its location.
srcdir="$tmp/unpacked"
if [ ! -f "$srcdir/amtr" ]; then
  inner="$(find "$srcdir" -maxdepth 2 -type f -name amtr 2>/dev/null | head -n1)"
  [ -n "$inner" ] || err "bundle did not contain the amtr binary"
  srcdir="$(dirname "$inner")"
fi

# ---------------------------------------------------------------------------
# install: whole bundle -> $PREFIX/lib/amtr, then symlink bin/amtr -> it
#
# The binary and amtr_engine.py end up in the SAME directory. bin/amtr is an
# absolute symlink into that directory. The engine is then found with no env var
# and no wrapper: on Linux via "engine next to the (symlink-resolved) exe", on
# macOS via ipc.rs's ../lib/amtr fallback (macOS current_exe does not resolve the
# symlink) — see the BUNDLEDIR note above.
# ---------------------------------------------------------------------------
rm -rf "$BUNDLEDIR"              # drop any prior bundle so stale files don't linger
mkdir -p "$BUNDLEDIR" "$BINDIR"
cp -R "$srcdir"/. "$BUNDLEDIR"/
chmod 0755 "$BUNDLEDIR/amtr"
ln -sf "$BUNDLEDIR/amtr" "$BINDIR/amtr"

note "Installed amtr $tag ($target)"
note "  bundle:  $BUNDLEDIR"
note "  binary:  $BINDIR/amtr -> $BUNDLEDIR/amtr"

# ---------------------------------------------------------------------------
# PATH note (warn, don't fail)
# ---------------------------------------------------------------------------
case ":$PATH:" in
  *":$BINDIR:"*) : ;;
  *)
    note ""
    note "NOTE: $BINDIR is not on your PATH. Add it so you can run 'amtr':"
    note "  echo 'export PATH=\"$BINDIR:\$PATH\"' >> ~/.profile   # or ~/.zshrc, ~/.bashrc"
    note "  then restart your shell."
    ;;
esac

# ---------------------------------------------------------------------------
# python check (warn, don't fail — the engine is a python3 child)
# ---------------------------------------------------------------------------
if ! have python3; then
  note ""
  note "WARNING: python3 not found. amtr's data engine needs python3 (>= 3.9, stdlib"
  note "only) at runtime. Install it from your package manager or https://python.org."
fi

note ""
note "Done. Try:  amtr --help"
note "(Report extras — the 'R' PDF report — additionally need: pip install matplotlib pillow, plus tectonic.)"
