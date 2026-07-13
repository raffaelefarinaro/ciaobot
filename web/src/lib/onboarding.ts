import type { GettingStartedProgress } from './gettingStarted'

export function isOnboardingFinished(
  progress: GettingStartedProgress,
  tourCompleted: boolean,
): boolean {
  return progress.allDone && tourCompleted
}
