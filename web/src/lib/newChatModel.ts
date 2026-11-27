/**
 * The model a new chat (Home's composer, a task hand-over) starts on, named
 * the way the chip and the picker show it. Pure: no Vue imports.
 *
 * With no explicit pick the chat follows the workspace default, which the
 * server resolves when it creates the chat. The PWA names that default by the
 * concrete model it is (`Claude Code · Opus 4.5`), never by the word
 * "default", and highlights it in the list as the current model. Picking that
 * same row while following the default keeps following it (nothing is
 * pinned); once a model is pinned, one last row offers the way back.
 */
import {
  displayedModel,
  modelName,
  providerForModelSection,
  providerModelLabel,
  providerShortLabel,
  sectionsFromModelsResponse,
  type ModelSection,
} from './modelSections'
import type { ModelsResponse, RuntimeProvider, WorkspaceInfo, WorkspaceProviderOption } from './types'

/** The model id of the "follow the workspace default" row. */
export const FOLLOW_DEFAULT_KEY = '__workspace-default__'
/** The section key of that row. */
export const FOLLOW_DEFAULT_SECTION = 'follow-default'

export interface ChosenRuntime {
  model: string
  provider: RuntimeProvider
}

/**
 * The provider and model a new chat in this workspace runs on when nothing is
 * picked: the workspace's provider, its model override for that provider,
 * else the provider's default model from the catalog.
 */
export function workspaceDefaultRuntime(
  response: ModelsResponse | null | undefined,
  workspace: WorkspaceInfo | null | undefined,
): { provider: string; model: string } {
  const provider = workspace?.default_provider || ''
  if (!provider) return { provider: '', model: '' }
  const model = workspace?.provider_overrides?.[provider]?.model?.trim()
    || response?.provider_defaults?.[provider]
    || ''
  return { provider, model }
}

export interface NewChatModelView {
  /** The chip's provider part, '' while nothing is known yet. */
  providerLabel: string
  /** The chip's model part, '' while the model has no name yet. */
  modelLabel: string
  /** The model the list highlights as current. */
  activeModel: string
  sections: ModelSection[]
}

export function newChatModelView(
  response: ModelsResponse | null | undefined,
  workspace: WorkspaceInfo | null | undefined,
  chosen: ChosenRuntime | null,
  providerOptions: WorkspaceProviderOption[] = [],
): NewChatModelView {
  const fallback = workspaceDefaultRuntime(response, workspace)
  const current = chosen ?? fallback
  const sections = sectionsFromModelsResponse(response ?? null)
  if (chosen && fallback.provider) {
    sections.push({
      key: FOLLOW_DEFAULT_SECTION,
      label: '',
      models: [FOLLOW_DEFAULT_KEY],
      modelLabels: { [FOLLOW_DEFAULT_KEY]: `Use workspace default (${providerModelLabel(response, fallback.provider, fallback.model)})` },
    })
  }
  return {
    // Before the catalog has loaded the workspace registry still names the
    // provider, so the chip shows it alone rather than a placeholder.
    providerLabel: current.provider
      ? (response?.providers?.length
        ? providerShortLabel(response, current.provider)
        : providerOptions.find(option => option.value === current.provider)?.short_label || current.provider)
      : '',
    modelLabel: current.provider && response ? modelName(response, current.provider, current.model) : '',
    // Claude Code's `default` is shown (and highlighted) as the concrete row
    // it stands for, when the catalog lists one.
    activeModel: current.provider ? displayedModel(response, current.provider, current.model) : current.model,
    sections,
  }
}

/**
 * What a pick means: null to follow the workspace default (the way-back row,
 * or the default's own row while already following it), else the pinned
 * runtime.
 */
export function chosenRuntimeFor(
  model: string,
  sectionKey: string,
  chosen: ChosenRuntime | null,
  response: ModelsResponse | null | undefined,
  workspace: WorkspaceInfo | null | undefined,
): ChosenRuntime | null {
  if (!model || model === FOLLOW_DEFAULT_KEY || sectionKey === FOLLOW_DEFAULT_SECTION) return null
  const provider = providerForModelSection(sectionKey)
  const fallback = workspaceDefaultRuntime(response, workspace)
  if (!chosen && fallback.provider === provider
    && (fallback.model === model || displayedModel(response, provider, fallback.model) === model)) return null
  return { model, provider }
}
