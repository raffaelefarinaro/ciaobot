import type { InjectionKey, Ref } from 'vue'

export interface ChoiceGroupContext {
  name: string
  modelValue: Ref<string>
  disabled: Ref<boolean>
  select: (value: string) => void
}

export const choiceGroupKey: InjectionKey<ChoiceGroupContext> = Symbol('onboarding-choice-group')
