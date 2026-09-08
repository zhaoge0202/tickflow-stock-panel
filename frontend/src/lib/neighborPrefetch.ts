import type { QueryClient } from '@tanstack/react-query'

/** Yield to mounted foreground queries, including charts that mount after daily K arrives. */
export function scheduleNeighborPrefetch(
  client: QueryClient,
  symbol: string,
  tasks: Array<() => Promise<unknown>>,
): () => void {
  let stopped = false
  let running = false
  let next = 0
  let timer: ReturnType<typeof setTimeout> | undefined

  const schedule = () => {
    if (stopped || running || timer !== undefined || next >= tasks.length) return
    timer = setTimeout(() => {
      timer = undefined
      if (stopped || client.isFetching({
        predicate: query => query.isActive() && query.queryKey.includes(symbol),
      })) return
      running = true
      void Promise.resolve().then(tasks[next++]).catch(() => {}).finally(() => {
        running = false
        schedule()
      })
    }, 0)
  }
  const unsubscribe = client.getQueryCache().subscribe(schedule)
  schedule()
  return () => {
    stopped = true
    clearTimeout(timer)
    unsubscribe()
  }
}
