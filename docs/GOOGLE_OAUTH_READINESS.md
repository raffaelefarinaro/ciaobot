# Shared Google OAuth readiness

Checked 2026-10-13. A Ciaobot-owned OAuth project is technically feasible, but
the current integration is not ready for a public shared-client rollout.
The intended default is a Ciaobot connection; a custom Google project remains
an explicit advanced option. Existing custom connections must keep working.

## Architecture decision

For installations with a browser on the engine computer, use a Desktop OAuth
client with PKCE and a loopback callback. Each installation obtains and stores
its own user tokens and calls Google directly through GWS. Project ownership
does not itself provide the maintainer with users' tokens or content.
Google treats installed applications as unable to keep client secrets:
[installed-app OAuth](https://developers.google.com/identity/protocols/oauth2/native-app).

The current remote-browser path uses manual paste-back because a phone's
localhost callback cannot reach an engine on another computer
(`SettingsView.vue::gwsOnEngineHost`). Connecting from the engine computer
can be the first supported shared-client flow. Seamless remote connection
needs a separate design; a distributed Web client secret is unsuitable.
Keep the signed session and same-origin gates on integration APIs.

## Findings and prerequisites

| Area | Current evidence | Required before public rollout |
|---|---|---|
| Scope selection | `gws_auth.py::scopes_for_profile` requests all nine core services for every account. | Persist per-account service choices, request only required permissions, handle partial grants and later permission upgrades. Gate skills and report missing permissions consistently. |
| Restricted permissions | Current `gmail.modify` and `drive` permissions are restricted. | Justify broad access and complete restricted-scope verification. Read-only mailbox or full-Drive access still uses restricted scopes. |
| Credential storage | `store_credentials` writes owner-private plaintext JSON and moves an existing encrypted store aside. | Implement protected token storage compatible with the installed GWS version and health monitor. Specify key protection and migration across macOS, Windows and headless Linux; preserve existing custom-client accounts. |
| Disconnect | `routes_api.py::gws_remove_profile` (`POST /api/integrations/gws/profiles/remove`) deletes the profile's local credential directory without calling Google's revocation endpoint. | Define local unlink versus Google grant revocation. Revocation can affect other connections for the same Google account and application; surface the effect and test it. |
| AI processing | Harnesses can receive Google tool output and send it to the user's model provider. | Document the complete data flow, provider retention/training terms, user consent, and prompt-injection protections. Include chats, archives, memory and backups in retention/deletion review. |
| Branding | Repository site has a product homepage, but no privacy-policy page or Limited Use statement was found. | Publish an accurate policy, link it from the homepage and app, verify domain ownership, configure support contacts and prepare the verification demonstration. |
| Shared operations | Distributed clients consume one project's quotas. | Assign project ownership, monitor quotas, review billing and abuse exposure, and document outages/suspension handling. Users need no project IAM roles. |

Google requires restricted-scope data and tokens to be encrypted at rest, key
protection, prompt-injection protection, and consent for appropriate transfers.
General-purpose model training is prohibited. These requirements also cover
derived data: [Workspace data policy](https://developers.google.com/workspace/workspace-api-user-data-developer-policy).

Cloud AI processing means Ciaobot should plan for the annual third-party
security assessment. A local-only exemption cannot be assumed; Google decides
the applicable verification path:
[restricted-scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification).

GWS documents encrypted credential storage, but Ciaobot's writer does not use
it today. Compatibility with the installed **0.22.5** binary must be exercised
before choosing a storage implementation; do not infer it from the upstream
main-branch documentation: [GWS authentication](https://github.com/googleworkspace/cli#authentication).

## API coverage and limits

Enable Gmail, Calendar, Drive, Docs, Sheets, Slides, Tasks, People and Forms
APIs in the shared project. This does not authorize every method or bypass
account/admin restrictions. Audit the scope needed by each supported command.

Replacing `drive` with `drive.file` changes the feature contract: access is
limited to files created by or explicitly shared with the app. It cannot
preserve arbitrary whole-Drive search:
[Drive scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth).

Gmail watch additionally needs Pub/Sub topics, subscriptions and IAM. A shared
OAuth client does not provision or isolate these resources. Keep cloud-resource
features in the advanced-project path until isolation and operations are
designed: [Gmail push notifications](https://developers.google.com/workspace/gmail/api/guides/push).

Testing is suitable for a controlled pilot: at most 100 listed testers and
seven-day refresh-token expiry for Workspace permissions. Publishing and
verification are distinct: [audience settings](https://support.google.com/cloud/answer/15549945).

New-project Gmail limits currently list 1,200,000 quota units per minute per
project and 6,000 per user per project. The documented daily billing threshold
is 80,000,000 units; billing rollout details remain pending. Verify actual
project limits before launch: [Gmail quotas](https://developers.google.com/workspace/gmail/api/reference/quota).
Also check [Calendar](https://developers.google.com/workspace/calendar/api/guides/quota)
and [Drive](https://developers.google.com/workspace/drive/api/guides/limits).

Brand verification needs an accessible homepage, linked same-domain privacy
policy and verified domain ownership:
[branding requirements](https://developers.google.com/identity/protocols/oauth2/production-readiness/brand-verification).

## Verification evidence

- 77 tests passed across `test_gws_auth.py`, `test_gws_relogin_routes.py`,
  `test_gws_auth_helper_pkce.py` and `test_gws_wrapper.py`.
- 95 tests passed across `test_workspace_settings_routes.py`,
  `test_auth_security.py` and `test_ws_auth.py`.
- Tests use synthetic credentials and mocked Google exchanges. Localhost
  listener tests passed outside the filesystem/network sandbox; the sandbox
  denied socket binding on the first attempt.
- Existing deprecation warnings concern Starlette/httpx and request cookies.
- No shared-project ID or OAuth client was supplied. Cloud Console API
  enablement, domain ownership, publishing/verification status and a real
  Google consent/token exchange remain unverified. No Google account grants
  or production credentials were changed.

Proceed with preparation and a controlled pilot after the storage and data-flow
decisions. Public availability remains contingent on those changes and Google's
approval; a custom project does not waive the data-use policy.
