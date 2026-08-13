// swift-tools-version:5.9
// amtrino — menu bar companion for amtr (SPEC f2 consumer).
// Build: swift build -c release   (see packaging/build-menubar.sh for the .app)
import PackageDescription

let package = Package(
    name: "AmtrBar",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "AmtrBar",
            path: "Sources/AmtrBar",
            resources: [.copy("Resources/amtr_engine.py")]
        ),
    ]
)
