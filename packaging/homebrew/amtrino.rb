# Cask for the amtrino menu bar companion. Lives in the tap
# (arian-shamaei/homebrew-anthropometer) next to the amtr formula; this copy
# is the source of truth, synced there on release.
#
# Release flow (until CI owns it): sh packaging/build-menubar.sh <version>,
# then `ditto -c -k --keepParent menubar/.build/amtrino.app amtrino-<version>.zip`,
# attach the zip to the GitHub release, and fill sha256 below.
cask "amtrino" do
  version "0.1.0"
  sha256 "41fc34a1c3f07ff14cbf1818ae1f36ca5204727aa2a53027a12b5e63ac73c605"

  url "https://github.com/arian-shamaei/anthropometer/releases/download/bar-v#{version}/amtrino-#{version}.zip"
  name "amtrino"
  desc "Menu bar companion for amtr: live Claude Code session dots and context gauge"
  homepage "https://github.com/arian-shamaei/anthropometer"

  depends_on macos: ">= :ventura"

  app "amtrino.app"

  zap trash: [
    "~/Library/Preferences/dev.arian-shamaei.amtrino.plist",
  ]
end
