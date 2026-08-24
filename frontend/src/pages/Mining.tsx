import { useState } from 'react'
import { BookmarkCheck } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { MiningWorkbench } from './backtest/MiningWorkbench'
import { ResearchCandidatesDialog } from './backtest/ResearchCandidatesDialog'

export function Mining() {
  const [candidatesOpen, setCandidatesOpen] = useState(false)

  return (
    <div className="flex min-h-full flex-col bg-base">
      <PageHeader
        title="挖掘"
        subtitle={<span className="hidden md:inline">嵌套样本外因子与策略挖掘</span>}
        className="shrink-0 flex-wrap gap-x-4 gap-y-2 bg-base/95 px-3 lg:flex-nowrap lg:px-5"
        right={(
          <button
            type="button"
            onClick={() => setCandidatesOpen(true)}
            aria-label="打开候选方案"
            title="候选方案"
            className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-btn border border-border bg-surface px-2 text-[11px] font-medium text-secondary transition-colors hover:border-accent/40 hover:text-accent sm:px-2.5 sm:text-xs"
          >
            <BookmarkCheck className="h-3.5 w-3.5" />
            <span>候选方案</span>
          </button>
        )}
      />

      <main className="min-h-0 flex-1 px-3 pb-3 pt-3 lg:px-4 lg:pb-4">
        <MiningWorkbench />
      </main>

      {candidatesOpen && <ResearchCandidatesDialog onClose={() => setCandidatesOpen(false)} />}
    </div>
  )
}
