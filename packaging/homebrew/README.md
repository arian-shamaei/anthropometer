# Homebrew packaging for `amtr`

`amtr` (brand: **anthropometer**) is a btop-style real-time diagnostic TUI for
Claude Code sessions. It is a hybrid: a Rust binary (`rust/`, crate `amtr`) that
owns the terminal UI, plus a stdlib-only Python data engine (`amtr_engine.py`)
that the binary spawns. The binary locates the engine via the `AMTR_ENGINE`
environment variable, which the Homebrew wrapper pins at install time.

This directory holds the tap formula and the shipping runbook. Nothing here has
been pushed — the formula still carries placeholders (see the TODOs below).

## Files

- `amtr.rb` — the formula (`class Amtr < Formula`).
- `README.md` — this runbook.

## Before you ship: placeholders to resolve

The formula is annotated with `TODO-*` comments. Resolve all three:

1. **`TODO-confirm(owner)`** — the GitHub owner handle `arian-shamaei` is a
   placeholder. If it is wrong, update `homepage`, `url`, and `head` together.
2. **`TODO-confirm(tag)` / version mismatch** — the formula targets `v0.1.0`,
   and `rust/Cargo.toml` is set to `version = "0.1.0"` to match. They must agree.
3. **`TODO-fill(sha256)`** — filled after the release tarball exists (step 2).

Also: **commit `rust/Cargo.lock`**. It is currently untracked. `std_cargo_args`
builds with `--locked`, so a tarball without `rust/Cargo.lock` fails the build.

## Shipping steps

### 1. Tag and create the GitHub release

```bash
cd /path/to/anthropometer
git add rust/Cargo.lock          # ensure the lockfile is in the release
git commit -am "amtr v0.1.0"
git tag v0.1.0
git push origin main --tags
gh release create v0.1.0 --title "amtr v0.1.0" \
  --notes "btop-style real-time context monitor for Claude Code sessions"
```

GitHub auto-generates the source tarball at:
`https://github.com/arian-shamaei/anthropometer/archive/refs/tags/v0.1.0.tar.gz`

### 2. Get the tarball SHA-256

```bash
curl -sL https://github.com/arian-shamaei/anthropometer/archive/refs/tags/v0.1.0.tar.gz \
  | shasum -a 256
# or, once url is set in the formula:  brew fetch --formula ./amtr.rb
```

Paste the hash into `amtr.rb`, replacing `TODO_FILL_AFTER_RELEASE`.

### 3. Create the tap repo

A tap is a GitHub repo named `homebrew-<tap>`; users tap it as `<owner>/<tap>`.
Here the tap is `anthropometer`, so the repo is **`homebrew-anthropometer`** and
the tap name is `arian-shamaei/anthropometer`.

```bash
gh repo create arian-shamaei/homebrew-anthropometer --public \
  --description "Homebrew tap for amtr (anthropometer)"
git clone https://github.com/arian-shamaei/homebrew-anthropometer
cd homebrew-anthropometer
mkdir -p Formula
cp /path/to/anthropometer/packaging/homebrew/amtr.rb Formula/amtr.rb
git add Formula/amtr.rb
git commit -m "amtr 0.1.0"
git push
```

### 4. Install

```bash
brew tap arian-shamaei/anthropometer
brew install amtr
amtr --help
```

`brew install amtr` builds the Rust binary and installs the Python engine. The
`amtr` command is a wrapper that sets `AMTR_ENGINE` and prepends python@3.12 to
`PATH`, so the engine is always found and always runs under the same
interpreter.

### 5. Enable the report/paper extras (optional)

The core monitor needs nothing else. The report wrappers — `amtr-paper`,
`amtr-gif`, and the PDF half of `amtr-report` — need matplotlib, Pillow, numpy
and tectonic. Install them into the **same** python@3.12 the formula uses:

```bash
"$(brew --prefix python@3.12)/libexec/bin/python3" -m pip install \
  --break-system-packages matplotlib pillow numpy
brew install tectonic
```

(Homebrew's python is externally managed — hence `--break-system-packages`. A
venv works too, but the wrappers call the interpreter by absolute path, so the
deps must live in that python's site-packages.) Until then the paper wrappers
exit with an `ImportError`; `amtr` and `amtr-report`'s markdown output are
unaffected. This is the intended "core-only formula, opt-in extras" split.

## The bin commands

| Command       | Backing module                    | Needs report extras? |
|---------------|-----------------------------------|----------------------|
| `amtr`        | Rust TUI + `amtr_engine.py`       | no                   |
| `amtr-report` | `amtr_engine.py --report`         | markdown no / PDF yes|
| `amtr-paper`  | `amtr_paper.py`                   | yes                  |
| `amtr-gif`    | `amtr_paper.py` (GIF pipeline)    | yes                  |

Only `amtr_paper.py` exposes a CLI; `amtr-gif` fronts the same pipeline because
the GIF animations are emitted as part of the paper build (there is no
standalone gif entrypoint).

## Local validation without pushing

```bash
# style + audit (audit's download check WILL fail on the placeholder tag/sha —
# that is expected until the release exists)
brew style packaging/homebrew/amtr.rb
brew audit --formula --new packaging/homebrew/amtr.rb

# full install/test dry run against a local tarball (temporarily point `url` at
# a file:// tarball with its real sha, then revert):
tar czf /tmp/amtr-0.1.0.tar.gz --exclude=.git --exclude=rust/target \
  --exclude=__pycache__ --exclude=.pytest_cache .
shasum -a 256 /tmp/amtr-0.1.0.tar.gz
brew install --build-from-source ./amtr.rb   # after editing url/sha locally
brew test amtr
```

## Why not homebrew-core?

`amtr` is **not** targeting `homebrew/core` yet. homebrew-core enforces a
notability bar (a well-known project with real traction / a stable release
history), which a brand-new tool does not meet. A personal tap
(`arian-shamaei/anthropometer`) is the right home until the project has that
track record. Revisit homebrew-core only after the project is established.
