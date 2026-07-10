import type { DataSourceItem, DataSourcesResponse, PluginDataSourceItem } from './api'

type DataSourceLike = DataSourceItem | PluginDataSourceItem

function pluginToItem(plugin: PluginDataSourceItem): DataSourceItem {
  return {
    name: plugin.name,
    display_name: plugin.display_name,
    datasets: plugin.datasets,
  }
}

export function allDataSourceItems(sources?: DataSourcesResponse): DataSourceItem[] {
  return [
    ...(sources?.builtin ?? []),
    ...(sources?.plugins ?? []).map(pluginToItem),
    ...(sources?.custom ?? []),
  ]
}

export function findDataSource(sources: DataSourcesResponse | undefined, name: string): DataSourceLike | null {
  return (
    (sources?.builtin ?? []).find(source => source.name === name)
    ?? (sources?.plugins ?? []).find(source => source.name === name)
    ?? (sources?.custom ?? []).find(source => source.name === name)
    ?? null
  )
}

export function dataSourceDisplayName(sources: DataSourcesResponse | undefined, name: string): string {
  if (name === 'tickflow') return 'TickFlow'
  return findDataSource(sources, name)?.display_name || name
}

export function dataSourceDatasets(sources: DataSourcesResponse | undefined, name: string): string[] {
  return findDataSource(sources, name)?.datasets ?? []
}

export function dataSourceSupportsDataset(
  sources: DataSourcesResponse | undefined,
  name: string,
  dataset: string,
): boolean {
  if (name === 'tickflow') return true
  const source = findDataSource(sources, name)
  if (!source) return false
  if ('available' in source && source.available === false) return false
  return source.datasets.includes(dataset)
}
