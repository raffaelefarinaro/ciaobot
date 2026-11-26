# Ordinary chat disclosure: authority boundary

Status: design for #3623; ordinary chat is not an authorized evidence source.

## Threat and required decision

Ciaobot runs agent tools as the engine's OS user, without an engine-enforced
filesystem sandbox. An agent can therefore read or alter engine-owned runtime
files, use a browser session credential it obtains, and make requests with
browser-like headers. A random local secret, a sealed Python object, a session
cookie, a WebSocket origin, or a user-role label cannot prove that a person
submitted a turn against this threat. Protecting only the ingress token is
insufficient: the attacker could instead change the grant, event, signing key,
or revocation state on disk. The signed structured-decision evidence mechanism
must not be assumed to provide this stronger boundary for chat.

There are two concrete paths to enable this feature:

1. **Isolate the authority and agent processes.** Run authority, its grant and
   evidence store, and its signing keys outside the agent's OS security domain;
   deny agents access to browser credentials and the authority channel. An
   independently protected operator UI submits each exact message to that
   authority. This can preserve one standing confirmation and ordinary chat,
   but requires a supported sandbox or separate OS account/service on each
   platform, including an installation, upgrade, recovery and accessibility
   design. It must be demonstrated against real agent tools and browser control.
2. **Require hardware-backed user verification for each eligible message.**
   A user-verified authenticator signs a challenge bound to the exact message,
   workspace, project, chat, destination and current grant generation. The
   authority verifies the signature and protected enrollment state. This avoids
   treating a reusable browser secret as authorship, but adds an interaction
   before each eligible send and requires a credible protection against local
   state rollback and tampering. It does not satisfy the requested ordinary
   send experience without an explicit product change.

Neither choice can be implemented by adding a bearer token to the existing
WebSocket. Until one has a reviewed implementation, sends use the existing
private chat path, no standing chat grant is offered, and no chat source event
or authorization is minted. No publication is enabled by this design.

## Selected direction and staged platform work

The selected direction is option 1. Its feasibility is conditional on an
installation and operator channel that actually keep agent processes outside
the authority's security domain. Merely starting another process under the
engine user does not do this. The authority must own its durable state and
signing material, authenticate its operator channel and the engine separately,
and reject untrusted callers before accepting any grant or turn. A compromised
engine account must not be able to rewrite that authority's executable,
configuration, state, credentials or service definition.

* **macOS:** (design: `docs/CHAT_AUTHORITY_HELPER.md`) investigate an installer-provisioned privileged service and
  dedicated service identity, together with an operator UI channel whose
  caller and user interaction the service can authenticate. A LaunchAgent
  under the engine user or a same-UID local socket is not a boundary. Service
  management, code signing, upgrades, protected state, host access and the
  interaction between the native app and remote PWA require platform review.
* **Linux:** investigate a system-installed service running under a dedicated
  account with root-owned executable and configuration, private persistent
  state and authenticated local IPC. A systemd user service under the engine
  account is not a boundary. A separate operator channel still needs an
  enforceable identity and proof of user action; filesystem permissions on
  the authority socket alone cannot distinguish a browser from an agent of
  the same UID. Remote PWA use and non-systemd installations need an explicit
  supported design or must remain private.
* **Other or unprovisioned hosts:** do not enable chat evidence. Absence,
  loss, downgrade or failed verification of the protected authority must
  disable grants and evidence admission, not fall back to same-UID state.

First validate each OS boundary with disposable installations and real
processes in separate security principals: test forged IPC, access to and
replacement of service files and state, restart, credential revocation, and
attempts by the agent principal to operate the UI channel. Then implement the
grant and immutable turn protocol in that authority, followed by integration
with the existing producer relations. Install and upgrade require administrative
provisioning and must disclose that requirement. On this development host a
noninteractive administrator credential is unavailable, so separate-account
installation and principal-isolation tests cannot be demonstrated here.

## Contract after the boundary exists

The authority must capture the exact plain UTF-8 composer bytes before provider
augmentation and associate them with server-resolved workspace, project and
chat lifetimes, a random immutable turn ID and revision, digest, principal,
grant generation and opt-out state. Attachments, forwarded or quoted content,
retrieval, tool messages and generated prompts are not attestable by origin or
role. The verifier still checks the complete original message; an operator
submission does not independently prove authorship of pasted text.

Standing grants are destination and audience bound, default off, scoped to
explicit chats or clearly disclosed future project chats, and limited to
approved fields and producer identities. The authority must serialize grant
changes, exclusions, event capture, revisions and withdrawal; retrying a send
ID with different bytes or scope is refused, while an identical retry resolves
to the same event. Revoke and withdrawal advance an epoch checked at every
source lookup and later at the #3619 publication admission boundary. A grant
cannot retroactively enroll older turns, survive workspace or link replacement,
or promise recall after publication.

Memory and care retain their engine-resolved `SourceRelation`. Evidence lookup
must recover only turns belonging to that relation, recheck the live grant and
withdrawal state, resolve the exact stored bytes and independently apply
`_project_vocabulary`. The intake receives its existing sealed authorization;
no model-provided text, offsets, author, destination or path is authority.
This requires a protected authority-store design as well as a protected ingress.

Before activation, isolated fixtures must exercise direct and queued sends,
replay, corrections, exclusions, stale UI, revoked and asymmetric grants,
foreign scopes, quoted or mixed text and forged browser credentials through
the real entry points. Independent security review must demonstrate that an
agent with the documented same-UID access cannot mint or modify an eligible
event, grant or withdrawal state, including after restart. #3624 and #3619 own
external effect and admission races; these tests must observe zero publication.
