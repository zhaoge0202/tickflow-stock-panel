import { Cable, ShieldAlert } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'

const NOTES = [
  {
    title: '系统边界',
    desc: '本项目只做提醒、解释、回放和人工处理记录, 不连接券商账户, 不发送委托。',
  },
  {
    title: '手动处理',
    desc: '盘中决策台里的“准备手动下单”只是状态标记, 实际操作仍由用户在券商软件中完成。',
  },
  {
    title: '复盘用途',
    desc: '这里后续只承接手动成交备注、风险复核和提醒表现复盘, 不建设自动交易桥接。',
  },
]

export function Trading() {
  return (
    <div className="flex h-full flex-col">
      <PageHeader title="交易" subtitle="人工处理记录 · 不自动下单" />

      <div className="flex-1 overflow-auto px-5 py-6">
        <div className="mx-auto max-w-3xl">
          <EmptyState
            icon={ShieldAlert}
            title="不接券商委托"
            hint="当前阶段只记录人工处理状态和复盘信息, 不提供自动下单、账户同步或券商交易接口。"
          />

          <section className="mt-6 rounded-card border border-border bg-surface p-5">
            <div className="mb-3 flex items-center gap-2">
              <Cable className="h-4 w-4 text-accent" />
              <h3 className="text-sm font-semibold text-foreground">边界说明</h3>
            </div>
            <ul className="space-y-3">
              {NOTES.map(item => (
                <li key={item.title} className="flex gap-3">
                  <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-accent" />
                  <div>
                    <p className="text-sm font-medium text-foreground">{item.title}</p>
                    <p className="mt-0.5 text-xs leading-relaxed text-secondary">{item.desc}</p>
                  </div>
                </li>
              ))}
            </ul>
          </section>
        </div>
      </div>
    </div>
  )
}
