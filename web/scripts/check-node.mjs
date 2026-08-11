/**
 * Fail fast when the running Node is older than jsdom's declared floor.
 *
 * Why this exists: jsdom 29 requires ^20.19.0 || ^22.13.0 || >=24, because its
 * CJS dependency chain does `require()` on an ESM module and `require(esm)` only
 * landed in Node 20.19.0. On an older Node every jsdom-environment test file
 * fails to start its worker — but vitest still prints "Test Files N passed" for
 * the files that *did* run, so the suite looks green while 17 of 42 files never
 * executed. A loud error beats a misleading summary.
 */

const MIN = [20, 19, 0]
const [major, minor, patch] = process.versions.node.split('.').map(Number)

function isSupported() {
  if (major > 20) return true
  if (major < 20) return false
  if (minor > 19) return true
  if (minor < 19) return false
  return patch >= MIN[2]
}

if (!isSupported()) {
  process.stderr.write(
    `\nNode ${process.versions.node} is too old to run the PWA test suite.\n` +
    `Need ^20.19.0 || ^22.13.0 || >=24.0.0 (see web/package.json engines).\n\n` +
    `On an older Node every jsdom test file silently fails to start, so the\n` +
    `summary reports a pass for the subset that ran. CI uses Node 22.\n\n` +
    `Fix: \`nvm use 22\` (see .nvmrc), or run with a newer Node on PATH.\n\n`,
  )
  process.exit(1)
}
