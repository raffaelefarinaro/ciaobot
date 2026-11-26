#!/bin/sh
# Build all spike binaries into ./build (run inside the macOS VM).
set -e
cd "$(dirname "$0")/.."
mkdir -p build
swiftc -O src/authority.swift -o build/authority 2>&1 | grep -v warning || true
swiftc -O src/app.swift -o build/app 2>&1 | grep -v warning || true
swiftc -O src/poster.swift -o build/poster 2>&1 | grep -v warning || true
clang -o build/tfp src/tfp.c
clang -dynamiclib -o build/inject.dylib src/inject.c
# operator app bundle, identity com.ciao.spike.app; plain ad-hoc and hardened-runtime variants
for v in plain hardened; do
  d=build/CiaoSpike-$v.app/Contents/MacOS; mkdir -p $d
  cp build/app $d/spikeapp
  cat > build/CiaoSpike-$v.app/Contents/Info.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>com.ciao.spike.app</string>
<key>CFBundleExecutable</key><string>spikeapp</string>
<key>CFBundlePackageType</key><string>APPL</string></dict></plist>
EOF
  if [ $v = hardened ]; then OPT="-o runtime"; else OPT=""; fi
  codesign -f -s - $OPT --identifier com.ciao.spike.app build/CiaoSpike-$v.app
done
codesign -f -s - build/inject.dylib
codesign -f -s - build/tfp; codesign -f -s - build/poster
ls build
