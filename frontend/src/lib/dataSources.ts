import type { DataSourcesResponse } from './api'

export function findDataSource(
  sources: DataSourcesResponse | undefined,
  name: string | undefined,
) {
  if (!sources || !name) return undefined
  return sources.builtin.find(source => source.name === name)
    ?? sources.plugins.find(source => source.name === name)
    ?? sources.custom.find(source => source.name === name)
}
