class Amtr < Formula
  desc "Btop-style real-time context monitor for Claude Code sessions"
  homepage "https://github.com/arian-shamaei/anthropometer"
  # TODO-confirm(tag): confirm the first release tag. NOTE: rust/Cargo.toml is
  url "https://github.com/arian-shamaei/anthropometer/archive/refs/tags/v0.1.3.tar.gz"
  # TODO-fill(sha256): filled AFTER the release exists. Get it with either
  #   shasum -a 256 v0.1.0.tar.gz        # on the downloaded release tarball
  #   brew fetch --formula ./amtr.rb     # brew prints the computed SHA-256
  sha256 "cbd84adeb802910e626912bc73f16e6cdf890ba6fc2b9135b01bee228f1bf05a"
  license "MIT"
  head "https://github.com/arian-shamaei/anthropometer.git", branch: "main"

  livecheck do
    url :stable
    strategy :github_latest
  end

  depends_on "rust" => :build
  # Runtime: the data engine (amtr_engine.py) is a python child the binary
  # spawns. matplotlib/Pillow/numpy/tectonic are deliberately NOT deps — the
  # report extras are opt-in (see `caveats`).
  depends_on "python@3.12"

  def install
    # 1. Build the Rust TUI. std_cargo_args passes --locked (so rust/Cargo.lock
    #    MUST ship in the tarball) and installs into the prefix => bin/"amtr".
    system "cargo", "install", *std_cargo_args(path: "rust")

    # 2. The stdlib-only engine plus the optional report modules it imports.
    libexec.install "amtr_engine.py", "amtr_paper.py", "amtr_figures.py",
                    "amtr_turns.py", "amtr_phases.py"

    # 3. Relocate the built binary under libexec and put a wrapper on PATH that
    #    (a) pins AMTR_ENGINE so the engine resolves regardless of the install
    #    prefix (the compile-time repo-relative fallback is wrong once
    #    installed), and (b) prepends python@3.12's unversioned bin so the
    #    engine — spawned as "python3" — and the report wrappers all run the
    #    SAME interpreter (one place to install the report extras).
    libexec.install bin/"amtr"
    python_bin = formula_opt_libexec("python@3.12")/"bin"
    (bin/"amtr").write_env_script libexec/"amtr",
                                  AMTR_ENGINE: libexec/"amtr_engine.py",
                                  PATH:        "#{python_bin}:$PATH"

    # 4. Report wrappers. Only amtr_paper.py exposes a CLI; the engine owns the
    #    plain --report. All run under python@3.12. They exit with an
    #    ImportError until the report extras are installed — that is expected.
    py = "#{python_bin}/python3"
    wrappers = {
      # compiled PDF paper + figures + GIF animations
      "amtr-paper"  => "#{py} #{libexec}/amtr_paper.py",
      # report directory: report.md always; PDF built in the background if extras present
      "amtr-report" => "#{py} #{libexec}/amtr_engine.py --report",
      # GIF animations are emitted by the paper pipeline (no standalone gif CLI)
      "amtr-gif"    => "#{py} #{libexec}/amtr_paper.py",
    }
    wrappers.each do |name, cmd|
      (bin/name).write <<~SH
        #!/bin/bash
        exec #{cmd} "$@"
      SH
      chmod 0755, bin/name
    end
  end

  def caveats
    <<~EOS
      The core monitor (`amtr`) works out of the box.

      The report/paper extras — `amtr-paper`, `amtr-gif`, and the PDF half of
      `amtr-report` — need matplotlib, Pillow, numpy and tectonic. Install them
      into the SAME python@3.12 this formula uses:

        "$(brew --prefix python@3.12)/libexec/bin/python3" -m pip install \\
          --break-system-packages matplotlib pillow numpy
        brew install tectonic

      (Homebrew's python is externally managed, hence --break-system-packages;
      a venv works too but the wrappers call the interpreter directly.)

      Until then the paper wrappers exit with an ImportError; `amtr` and
      `amtr-report`'s markdown output are unaffected.
    EOS
  end

  test do
    # 1. Binary + env-script wrapper resolve and self-describe. `--help` returns
    #    0 BEFORE spawning the engine or touching the terminal, so it is fully
    #    input-free, and its usage text names the target platform.
    assert_match "Claude Code", shell_output("#{bin}/amtr --help")

    # 2. Functional: feed the engine a synthetic Claude Code transcript and
    #    assert it parses it into a JSON report — the real data path, no TUI.
    (testpath/"session.jsonl").write <<~JSONL
      {"parentUuid":null,"isSidechain":false,"userType":"external","cwd":"/tmp/p","sessionId":"feedbeef-0000-4000-8000-1234567890ab","version":"2.1.205","gitBranch":"main","entrypoint":"cli","type":"user","uuid":"u-1","timestamp":"2026-07-17T10:00:07.000Z","message":{"role":"user","content":"hello claude"}}
      {"parentUuid":"u-1","isSidechain":false,"userType":"external","cwd":"/tmp/p","sessionId":"feedbeef-0000-4000-8000-1234567890ab","version":"2.1.205","gitBranch":"main","entrypoint":"cli","type":"assistant","uuid":"a-1","requestId":"req_001","timestamp":"2026-07-17T10:00:14.000Z","message":{"role":"assistant","model":"claude-fable-5","content":[{"type":"text","text":"hi there"}],"usage":{"input_tokens":10,"output_tokens":5,"cache_read_input_tokens":1000,"cache_creation_input_tokens":2000}}}
    JSONL
    py = formula_opt_libexec("python@3.12")/"bin/python3"
    report = shell_output("#{py} #{libexec}/amtr_engine.py --report --json " \
                          "--session #{testpath}/session.jsonl")
    assert_match "\"session_id\"", report
    assert_match(/"turns":\s*1/, report)
  end
end
