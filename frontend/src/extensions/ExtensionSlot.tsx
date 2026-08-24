import type { FrontendSlotContextMap, FrontendSlotName } from './types'
import { getFrontendSlotRegistrations } from './registry'
import { ExtensionBoundary } from './ExtensionBoundary'

type Props<K extends FrontendSlotName> = {
  name: K
  context: FrontendSlotContextMap[K]
  compact?: boolean
}

export function ExtensionSlot<K extends FrontendSlotName>({ name, context, compact }: Props<K>) {
  const registrations = getFrontendSlotRegistrations(name)
  if (registrations.length === 0) return null
  return registrations.map(registration => {
    const SlotComponent = registration.component
    return (
      <ExtensionBoundary
        key={`${registration.extensionId}:${registration.id}`}
        extensionId={registration.extensionId}
        compact={compact}
      >
        <SlotComponent {...context} />
      </ExtensionBoundary>
    )
  })
}
