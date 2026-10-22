// The Google account id a workspace's Connect dialog registers. It is derived
// from the workspace name and never typed; the server re-validates every id.

/** Mirrors ciao/gws_auth.py GWS_SERVICE_NAMES; tests/test_gws_auth.py compares them. */
export const GWS_RESERVED_PROFILE_NAMES: readonly string[] = ['gmail', 'calendar', 'drive', 'docs', 'sheets', 'slides', 'tasks', 'contacts', 'forms', 'auth']

/** Mirrors gws_auth.slugify_profile: lower, trim, [^a-z0-9_-]+ -> '-', strip '-'. */
export function slugifyGwsProfile(name: string): string {
  return String(name).trim().toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '')
}

/** base = slug || 'workspace'; reserved -> `${base}-google`; while taken.has(id): `${base}-2`, `-3`, ... */
export function deriveGwsProfileId(workspaceName: string, taken: ReadonlySet<string>): string {
  let base = slugifyGwsProfile(workspaceName) || 'workspace'
  if (GWS_RESERVED_PROFILE_NAMES.includes(base)) base = `${base}-google`
  if (!taken.has(base)) return base
  for (let n = 2; ; n += 1) {
    const id = `${base}-${n}`
    if (!taken.has(id)) return id
  }
}

/**
 * Where the account's files live, for display before the profile exists:
 * personal -> 'secrets/gws-personal/', work -> 'secrets/gws/', else `secrets/gws-${id}/`.
 */
export function gwsStorageHint(profileId: string): string {
  if (profileId === 'personal') return 'secrets/gws-personal/'
  if (profileId === 'work') return 'secrets/gws/'
  return `secrets/gws-${profileId}/`
}
