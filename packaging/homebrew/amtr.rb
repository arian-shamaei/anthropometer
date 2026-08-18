class Amtr < Formula
  desc "Agentic debugger for Claude Code: live context map, replay, subagents, post-mortems"
  homepage "https://github.com/arian-shamaei/anthropometer"
  # TODO-confirm(tag): confirm the first release tag. NOTE: rust/Cargo.toml is
  url "https://github.com/arian-shamaei/anthropometer/archive/refs/tags/v0.5.0.tar.gz"
  # TODO-fill(sha256): filled AFTER the release exists. Get it with either
  #   shasum -a 256 v0.1.0.tar.gz        # on the downloaded release tarball
  #   brew fetch --formula ./amtr.rb     # brew prints the computed SHA-256
  sha256 "fe1ccd184bed07d88e2a370062f10dc3a18674e5d596ad12e52422d91800bdda"
  license "MIT"
  head "https://github.com/arian-shamaei/anthropometer.git", branch: "main"

  livecheck do
    url :stable
    strategy :github_latest
  end

  depends_on "rust" => :build
  # Runtime: the data engine (amtr_engine.py) is a stdlib-only python child
  # the binary spawns. The PDF/figures report builder is a SEPARATE package
  # (`pip install amtr-paper`) the engine discovers on PATH — deliberately
  # not a dep (see `caveats`).
  depends_on "python@3.12"

  def install
    # 1. Build the Rust TUI. std_cargo_args passes --locked (so rust/Cargo.lock
    #    MUST ship in the tarball) and installs into the prefix => bin/"amtr".
    system "cargo", "install", *std_cargo_args(path: "rust")

    # 2. The stdlib-only engine. The report modules are NOT installed here —
    #    they ship as the amtr-report pip package.
    libexec.install "amtr_engine.py"

    # 3. Relocate the built binary under libexec and put a wrapper on PATH that
    #    (a) pins AMTR_ENGINE so the engine resolves regardless of the install
    #    prefix (the compile-time repo-relative fallback is wrong once
    #    installed), and (b) prepends python@3.12's unversioned bin so the
    #    engine — spawned as "python3" — runs a known interpreter.
    libexec.install bin/"amtr"
    python_bin = formula_opt_libexec("python@3.12")/"bin"
    (bin/"amtr").write_env_script libexec/"amtr",
                                  AMTR_ENGINE: libexec/"amtr_engine.py",
                                  PATH:        "#{python_bin}:$PATH"

    # 4. Markdown report wrapper (stdlib, always works — including --watch).
    #    The compiled PDF/figures builder is NOT here: it is the separate
    #    `amtr-paper` pip package, which the engine discovers on PATH.
    py = "#{python_bin}/python3"
    (bin/"amtr-report").write <<~SH
      #!/bin/bash
      exec #{py} #{libexec}/amtr_engine.py --report "$@"
    SH
    chmod 0755, bin/"amtr-report"
  end

  def caveats
    <<~EOS
      The core monitor (`amtr`), the markdown report (`amtr-report`), and
      the instant half of the in-TUI `R` key work out of the box.

      The compiled PDF + figures report is a separate plugin. To get it
      (adds `amtr-paper`, which the `R` key auto-discovers):

        pip install amtr-paper
        brew install tectonic        # LaTeX -> PDF

      Without the plugin, `R` still writes report.md and tells you what is
      missing.
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
