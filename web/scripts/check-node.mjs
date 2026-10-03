/**
 * Fail fast when the running Node is older than jsdom's declared floor.
 *
 * Why this exists: jsdom 30 requires ^22.22.2 || ^24.15.0 || >=26, and every
 * jsdom-environment test file fails to start its worker below that — but vitest
 * still prints "Test Files N passed" for the files that *did* run, so the suite
 * looks green while most of the jsdom files never executed. A loud error beats
 * a misleading summary.
 *
 * The floor moved when jsdom did. jsdom 29 asked for
 * ^20.19.0 || ^22.13.0 || >=24.0.0, which is why Node 20 was ever supported
 * here: its CJS dependency chain `require()`d an ESM module, and `require(esm)`
 * only landed in Node 20.19.0. jsdom 30 raised all three lines and dropped the
 * 20.x one entirely — Node 20 is EOL, so nothing is lost by following it.
 */

export const SUPPORTED_RANGE = '^22.22.2 || ^24.15.0 || >=26.0.0'

/**
 * Mirrors SUPPORTED_RANGE exactly.
 *
 * A bare `major > 22` check is wrong — it waves through 22.0–22.21, 23.x, 24.0–24.14
 * and 25.x, none of which satisfy the range. 22.0–22.21 in particular lacks the
 * unflagged `require(esm)` this gate exists to require, and 23.x/25.x are dead
 * odd-numbered lines with no qualifying release.
 */
export function isSupportedVersion(version) {
  const [major, minor, patch] = String(version).split('.').map(Number)
  if (![major, minor, patch].every(Number.isInteger)) return false

  const atLeast = (a, b, c) => {
    if (major !== a) return major > a
    if (minor !== b) return minor > b
    return patch >= c
  }

  if (major === 22) return atLeast(22, 22, 2)
  if (major === 24) return atLeast(24, 15, 0)
  // 23 and 25 are dead odd-numbered lines with no qualifying release.
  return major >= 26
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
