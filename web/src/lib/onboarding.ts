export type OnboardingStep = 'location' | 'runtime' | 'access_memory' | 'care' | 'finish'
export type OnboardingStatus = 'in_progress' | 'completed' | 'legacy_exempt'
export type OnboardingTarget = { kind: 'installation' } | { kind: 'workspace'; name: string }

export interface OnboardingReceipt {
  schema: 1
  revision: number
  installation: { status: OnboardingStatus; step: OnboardingStep }
  workspaces: Record<string, { status: OnboardingStatus; step: OnboardingStep }>
}

export interface OnboardingStageContext {
  target: OnboardingTarget
  workspace: string
  revision: number
  provisioned: boolean
}

export interface StageState {
  dirty: boolean
  pending: boolean
  canContinue: boolean
  error: string | null
}

export interface OnboardingSummaryItem {
  id: 'location' | 'backup' | 'runtime' | 'model' | 'file_access' | 'approvals' | 'memory' | 'care'
  label: string
  value: string
  state: 'ready' | 'deferred' | 'attention' | 'unknown'
  href?: string
}

export const ONBOARDING_STEPS: { id: OnboardingStep; label: string }[] = [
  { id: 'location', label: 'Location' },
  { id: 'runtime', label: 'Runtime' },
  { id: 'access_memory', label: 'Access & memory' },
  { id: 'care', label: 'Care' },
  { id: 'finish', label: 'Finish' },
]

export interface OnboardingStageProps { context: OnboardingStageContext; busy: boolean }
export type OnboardingStageEvents = { state: [state: StageState]; continue: []; back: []; refresh: [] }

export function onboardingProgress(receipt: OnboardingReceipt | null, target: OnboardingTarget) {
  if (!receipt) return null
  return target.kind === 'installation' ? receipt.installation : receipt.workspaces[target.name] ?? null
}

/**
 * Whether the shell may advance from `step`. The shared access/memory screen
 * has two owners, so it advances only when both are ready and neither is busy
 * or reporting an error; every other step follows its own stage's readiness.
 */
export function readyToAdvance(
  step: OnboardingStep,
  shell: StageState,
  access?: StageState | null,
  memory?: StageState | null,
): boolean {
  if (step !== 'access_memory') return shell.canContinue
  if (!access || !memory) return false
  const settled = (state: StageState) => state.canContinue && !state.pending && !state.error
  return settled(access) && settled(memory)
}
