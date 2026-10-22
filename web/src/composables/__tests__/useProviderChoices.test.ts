import { describe, expect, it } from 'vitest'
import { ref } from 'vue'
import type { ModelsResponse, RoutineSettings } from '../../lib/types'
import {
  DEFAULT_MODEL_SELECTION,
  DEFAULT_THINKING_SELECTION,
  useProviderChoices,
} from '../useProviderChoices'

describe('useProviderChoices', () => {
  it('builds a Default section naming the engine default', () => {
    const routines = ref({
      model_options: { anthropic: ['opus'] },
      provider_default_models: { claude: 'opus' },
    } as unknown as RoutineSettings)
    const workspaceModels = ref<ModelsResponse | null>(null)
    const { providerSectionsWithDefault, providerThinkingOptions } = useProviderChoices({
      routines,
      workspaceModels,
    })
    const sections = providerSectionsWithDefault(
      'claude',
      DEFAULT_MODEL_SELECTION,
      (effective) => `Automatic (${effective})`,
      true,
    )
    expect(sections[0].modelLabels?.[DEFAULT_MODEL_SELECTION]).toBe('Automatic (opus)')
    expect(providerThinkingOptions('claude')[0].value).toBe(DEFAULT_THINKING_SELECTION)
  })
})
