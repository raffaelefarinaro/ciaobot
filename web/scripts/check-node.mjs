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

export const SUPPORTED_RANGE = '^20.19.0 || ^22.13.0 || >=24.0.0'

/**
 * Mirrors SUPPORTED_RANGE exactly.
 *
 * A bare `major > 20` check is wrong — it waves through 21.x, 22.0–22.12 and
 * 23.x, none of which satisfy the range, and 22.0–22.12 in particular lacks the
 * unflagged `require(esm)` this gate exists to require.
 */
export function isSupportedVersion(version) {
  const [major, minor, patch] = String(version).split('.').map(Number)
  if (![major, minor, patch].every(Number.isInteger)) return false

  const atLeast = (a, b, c) => {
    if (major !== a) return major > a
    if (minor !== b) return minor > b
    return patch >= c
  }

  if (major === 20) return atLeast(20, 19, 0)
  if (major === 22) return atLeast(22, 13, 0)
  // 21 and 23 are dead odd-numbered lines with no qualifying release.
  return major >= 24
}

// Only act when run as the entry point, so importing this from a test is inert.
if (process.argv[1] && import.meta.url === `file://${process.argv[1]}`) {
  if (!isSupportedVersion(process.versions.node)) {
    process.stderr.write(
      `\nNode ${process.versions.node} is too old to run the PWA test suite.\n` +
      `Need ${SUPPORTED_RANGE} (see web/package.json engines).\n\n` +
      `On an unsupported Node every jsdom test file silently fails to start, so\n` +
      `the summary reports a pass for the subset that ran. CI uses Node 22.\n\n` +
      `Fix: \`nvm use 22\` (see .nvmrc), or run with a newer Node on PATH.\n\n`,
    )
    process.exit(1)
  }
}
