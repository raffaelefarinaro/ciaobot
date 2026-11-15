// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "CiaobotMac",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "CiaobotMac", targets: ["CiaobotMac"])
    ],
    dependencies: [
        // Exactly pinned: Sparkle's prebuilt framework and its nested helpers
        // are packaged by scripts/build-app.sh and signed by
        // scripts/sign-sparkle.sh. Resolving the dependency needs network
        // access; Package.resolved records the resolved revision.
        .package(url: "https://github.com/sparkle-project/Sparkle", exact: "2.10.1"),
    ],
    targets: [
        // The face and app-icon artwork ship with the client. It is
        // deliberately a package resource so a debug build and the
        // hand-assembled release .app do not depend on the Ciaobot engine
        // checkout at runtime.
        .executableTarget(
            name: "CiaobotMac",
            dependencies: [.product(name: "Sparkle", package: "Sparkle")],
            exclude: ["Artwork"],
            resources: [.process("Resources")],
            linkerSettings: [
                // The hand-assembled bundle copies Sparkle.framework into
                // Contents/Frameworks; resolve it relative to the executable
                // rather than the SwiftPM build directory.
                .unsafeFlags(["-Xlinker", "-rpath", "-Xlinker", "@executable_path/../Frameworks"]),
                // `swift build` places Sparkle.framework beside the debug and
                // release executables, and the GUI launch test runs that
                // executable directly. This path is inert inside the .app.
                .unsafeFlags(["-Xlinker", "-rpath", "-Xlinker", "@executable_path"]),
            ]
        ),
        .testTarget(
            name: "CiaobotMacTests",
            dependencies: ["CiaobotMac"],
            linkerSettings: [
                // The test bundle links the executable's Sparkle dependency.
                // SwiftPM keeps Sparkle.framework beside the test bundle in the
                // build products directory but does not add a runpath to it,
                // so point at that directory relative to the runner.
                .unsafeFlags(["-Xlinker", "-rpath", "-Xlinker", "@loader_path/../../.."]),
            ]
        ),
    ]
)
