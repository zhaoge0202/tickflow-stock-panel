import type { FrontendExtensionModule } from './types'
import { loadFrontendExtensions } from './registry'

const modules = import.meta.glob<FrontendExtensionModule>(
  '../custom/*/extension.tsx',
)

export async function initializeFrontendExtensions() {
  await loadFrontendExtensions(modules)
}
