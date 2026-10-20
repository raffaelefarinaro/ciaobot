// Code mode helpers. Code mode is one-way per chat: once the chat has messages
// it cannot be turned off (the engine refuses), so the switch locks on.
import type { ProjectRepo } from './types'

/** Mirrors the engine rule `chat.code_mode and project.repo_path`. */
export function isChatCodeModeOn(
  chat: { code_mode?: boolean } | null | undefined,
  repo: ProjectRepo | null | undefined,
): boolean {
  return Boolean(chat?.code_mode) && Boolean(repo)
}

/** Mirrors the engine refusal: on, and the chat already has a user turn. */
export function isCodeModeLocked(
  chat: { code_mode?: boolean; user_turn_count?: number } | null | undefined,
): boolean {
  return Boolean(chat?.code_mode) && (chat?.user_turn_count ?? 0) > 0
}
