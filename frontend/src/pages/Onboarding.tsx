import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Loader2,
  CheckCircle2,
  AlertCircle,
  ArrowRight,
  ArrowLeft,
  Sparkles,
  LineChart,
  ScanSearch,
  Flame,
  Zap,
  Radar,
  ShieldCheck,
  BellRing,
  TrendingUp,
  FileText,
  Landmark,
  Database,
  Plus,
  Puzzle,
} from 'lucide-react'
import { api } from '@/lib/api'
import { useCapabilities, usePreferences, useSettings } from '@/lib/useSharedQueries'
import { QK } from '@/lib/queryKeys'
import { CAP_LABELS } from '@/lib/capability-labels'
import { Logo } from '@/components/Logo'
import { DataSourceEditor } from '@/pages/settings/DataSourceEditor'

// ===== 引导页:5 步向导 =====
// 0. 声明  1. 欢迎  2. 输入 Key(可跳过)  3. 能力探测结果  4. 完成 → 写标记 → 进面板

const STEPS = ['声明', '欢迎', '数据源', '能力探测', '完成'] as const

const BRAND = '#8B5CF6'

const HIGHLIGHTS = [
  { icon: LineChart,   title: '看板与自选', desc: '市场全景看板、涨跌分布、情绪雷达,自定义自选列表', tint: 'text-accent' },
  { icon: ScanSearch,  title: '策略选股',   desc: '内置多套选股策略,一键扫描全市场命中标的', tint: 'text-bull' },
  { icon: TrendingUp,  title: '个股分析',   desc: 'AI 四维分析个股,关键价位、技术形态一目了然', tint: 'text-warning' },
  { icon: Flame,       title: '连板梯队',   desc: '涨停梯队、封板强度、炸板监控,情绪温度计', tint: 'text-warning' },
  { icon: Landmark,    title: '概念行业',   desc: '概念板块、行业维度的资金流向与热度排名', tint: 'text-accent' },
  { icon: FileText,    title: '财务分析',   desc: 'AI 解读财报,利润、资负、现金流、核心指标', tint: 'text-bear' },
  { icon: ShieldCheck, title: '回测验证',   desc: '策略历史回测、因子分析,用数据验证逻辑', tint: 'text-accent' },
  { icon: Radar,       title: '实时监控',   desc: '自定义条件 / 策略监控,盘中触发即推送告警', tint: 'text-bear' },
  { icon: BellRing,    title: '本地优先',   desc: '数据本地存储,隐私可控,断网仍可查阅', tint: 'text-bull' },
]

export function Onboarding() {
  const navigate = useNavigate()
  const qc = useQueryClient()

  const [step, setStep] = useState(0)

  // 完成向导 —— 写后端标记,使守卫放行
  const complete = useMutation({
    mutationFn: api.completeOnboarding,
    onSuccess: (data) => {
      // 用接口返回值同步更新缓存,确保跳转时守卫立即看到 onboarding_completed: true
      // (避免 invalidate 后台重取未返回时, 守卫用旧缓存 false 误重定向回引导页)
      qc.setQueryData(QK.settings, (old: any) =>
        old ? { ...old, onboarding_completed: data.onboarding_completed } : old,
      )
      qc.invalidateQueries({ queryKey: QK.settings })
      navigate('/', { replace: true })
    },
    onError: () => {
      // 标记失败不应阻塞用户进入面板,仍放行
      navigate('/', { replace: true })
    },
  })

  const finish = () => complete.mutate()

  return (
    <div className="relative min-h-screen bg-base overflow-hidden flex flex-col">
      {/* 背景光晕 —— 品牌 + 主色渐变 */}
      <div className="pointer-events-none absolute inset-0 overflow-hidden">
        <div
          className="absolute -top-40 -left-40 h-[28rem] w-[28rem] rounded-full blur-[120px] opacity-20"
          style={{ background: `radial-gradient(circle, ${BRAND}, transparent 70%)` }}
        />
        <div
          className="absolute -bottom-40 -right-32 h-[26rem] w-[26rem] rounded-full blur-[120px] opacity-15"
          style={{ background: 'radial-gradient(circle, hsl(var(--accent)), transparent 70%)' }}
        />
        {/* 极淡网格底纹 */}
        <div
          className="absolute inset-0 opacity-[0.025]"
          style={{
            backgroundImage:
              'linear-gradient(hsl(var(--fg-primary)) 1px, transparent 1px), linear-gradient(90deg, hsl(var(--fg-primary)) 1px, transparent 1px)',
            backgroundSize: '40px 40px',
          }}
        />
      </div>

      {/* 顶栏:logo + 进度指示 */}
      <header className="relative z-10 flex items-center justify-between px-6 py-4 border-b border-border">
        <div className="flex items-center gap-2.5 text-foreground">
          <Logo
            size={24}
            className="shrink-0"
            style={{ color: BRAND, filter: `drop-shadow(0 0 8px ${BRAND}55)` }}
          />
          <span className="text-sm font-semibold tracking-tight">Tick Stock Panel</span>
        </div>
        {/* 步骤进度条 —— 胶囊式 */}
        <div className="flex items-center gap-1.5">
          {STEPS.map((label, i) => (
            <div key={label} className="flex items-center gap-1.5">
              {i > 0 && <div className="h-px w-3 bg-border" />}
              <motion.div
                animate={{
                  width: i === step ? 64 : 24,
                  backgroundColor: i === step
                    ? 'hsl(var(--accent))'
                    : i < step
                      ? 'hsl(var(--accent) / 0.6)'
                      : 'hsl(var(--border))',
                }}
                transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
                className="h-1.5 rounded-full"
              />
            </div>
          ))}
        </div>
        <div className="w-[88px] text-right">
          <span className="text-xs text-muted tabular">
            {step + 1} / {STEPS.length}
          </span>
        </div>
      </header>

      {/* 步骤内容 (数据源步骤含编辑器, 加宽容器) */}
      <main className="relative z-10 flex-1 flex items-center justify-center px-6 py-10">
        <div className={`w-full ${step === 2 ? 'max-w-3xl' : 'max-w-xl'}`}>
          <AnimatePresence mode="wait">
            <motion.div
              key={step}
              initial={{ opacity: 0, x: 24 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -24 }}
              transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
            >
              {step === 0 && <DisclaimerStep onNext={() => setStep(1)} />}
              {step === 1 && <WelcomeStep onNext={() => setStep(2)} onSkip={finish} />}
              {step === 2 && (
                <DataSourceStep onNext={() => setStep(3)} onBack={() => setStep(1)} />
              )}
              {step === 3 && <ResultStep onNext={() => setStep(4)} onBack={() => setStep(2)} />}
              {step === 4 && <FinishStep onNext={finish} onBack={() => setStep(3)} pending={complete.isPending} />}
            </motion.div>
          </AnimatePresence>
        </div>
      </main>
    </div>
  )
}

// ===== Step 0: 声明 =====

function DisclaimerStep({ onNext }: { onNext: () => void }) {
  return (
    <div className="text-center">
      <motion.div
        initial={{ scale: 0.85, opacity: 0 }}
        animate={{ scale: 1, opacity: 1 }}
        transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
        className="mx-auto w-fit rounded-2xl p-4 border border-warning/40"
        style={{ background: 'linear-gradient(135deg, hsl(var(--warning) / 0.15), transparent)' }}
      >
        <AlertCircle className="h-8 w-8 text-warning" />
      </motion.div>

      <h1 className="mt-6 text-2xl font-bold text-foreground tracking-tight">使用前请知悉</h1>

      <div className="mt-5 rounded-card border border-border bg-surface/80 backdrop-blur-sm p-5 text-left">
        <div className="flex items-start gap-2.5">
          <ShieldCheck className="h-4 w-4 text-accent shrink-0 mt-0.5" />
          <div className="space-y-2.5 text-sm text-secondary leading-relaxed">
            <p>
              本项目为<strong className="text-warning">个人开源项目</strong>,由个人独立维护,与任何商业数据服务
              <span className="text-warning">无官方关联</span>。数据能力依赖第三方数据服务提供。
            </p>
            <p>
              仅供学习研究使用,不构成任何投资建议。股市有风险,使用本项目产生的任何盈亏由使用者自行承担。
            </p>
            <p>
              本项目基于 MIT 协议开源。使用本项目时,请遵守所用数据源的服务条款;第三方接口插件存在版权与反爬风险,使用需自行评估合规责任。
            </p>
          </div>
        </div>
      </div>

      <div className="mt-6 flex items-center justify-center">
        <button
          onClick={onNext}
          className="inline-flex items-center gap-2 px-6 h-11 rounded-xl bg-accent text-white text-sm font-semibold shadow-lg shadow-accent/20 hover:bg-accent/90 hover:shadow-accent/30 transition-all"
        >
          我已了解,继续
          <ArrowRight className="h-4 w-4" />
        </button>
      </div>
    </div>
  )
}

// ===== Step 1: 欢迎 =====

function WelcomeStep({ onNext, onSkip }: { onNext: () => void; onSkip: () => void }) {
  return (
    <div className="text-center">
      {/* 品牌 badge */}
      <motion.div
        initial={{ scale: 0.85, opacity: 0 }}
        animate={{ scale: 1, opacity: 1 }}
        transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
        className="mx-auto w-fit rounded-2xl p-4 border border-border"
        style={{ background: `linear-gradient(135deg, ${BRAND}22, transparent)` }}
      >
        <Sparkles className="h-8 w-8" style={{ color: BRAND }} />
      </motion.div>

      <h1 className="mt-6 text-3xl font-bold text-foreground tracking-tight">
        欢迎使用 TSP
      </h1>
      <p className="mt-3 text-sm text-secondary leading-relaxed max-w-md mx-auto">
        一个本地化的 A 股量化分析面板 —— 行情、选股、回测、监控、财务一体化。
        花一分钟配置,即可开始使用。
      </p>

      {/* 特性卡片 —— 3×3 网格,横向布局压缩高度 */}
      <div className="mt-8 grid grid-cols-2 sm:grid-cols-3 gap-2.5 text-left">
        {HIGHLIGHTS.map((h, i) => (
          <motion.div
            key={h.title}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.3, delay: 0.04 * i + 0.1 }}
            whileHover={{ y: -2 }}
            className="group flex items-start gap-2.5 rounded-card border border-border bg-surface/80 backdrop-blur-sm p-2.5 transition-colors hover:border-accent/30"
          >
            <div className="rounded-lg bg-elevated/50 p-1.5 shrink-0">
              <h.icon className={`h-4 w-4 ${h.tint} transition-transform group-hover:scale-110`} />
            </div>
            <div className="min-w-0">
              <div className="text-xs font-medium text-foreground">{h.title}</div>
              <div className="mt-0.5 text-[11px] text-muted leading-snug line-clamp-2">{h.desc}</div>
            </div>
          </motion.div>
        ))}
      </div>

      <div className="mt-8 flex items-center justify-center gap-3">
        <button
          onClick={onNext}
          className="inline-flex items-center gap-2 px-6 h-11 rounded-xl bg-accent text-white text-sm font-semibold shadow-lg shadow-accent/20 hover:bg-accent/90 hover:shadow-accent/30 transition-all"
        >
          开始配置
          <ArrowRight className="h-4 w-4" />
        </button>
        <button
          onClick={onSkip}
          className="px-4 h-11 rounded-xl text-sm text-secondary hover:text-foreground hover:bg-elevated transition-colors"
        >
          稍后再说
        </button>
      </div>
    </div>
  )
}

// ===== Step 2: 配置第三方数据源 (默认内置源; 可添加自有数据源) =====

/** datasets 标签的中文名 (与设置页数据集口径一致) */
const DATASET_LABELS: Record<string, string> = {
  daily: '日K',
  adj_factor: '除权',
  realtime: '实时',
  minute: '分钟K',
  financial: '财务',
}

function DataSourceStep({ onNext, onBack }: { onNext: () => void; onBack: () => void }) {
  const qc = useQueryClient()
  const prefs = usePreferences()
  const sources = useQuery({
    queryKey: QK.dataSources,
    queryFn: api.dataSources,
    staleTime: 60_000,
  })

  // null = 跟随后端当前激活源 (首次使用即默认内置源)
  const [picked, setPicked] = useState<string | null>(null)
  // 是否展开「添加自有数据源」编辑器
  const [adding, setAdding] = useState(false)

  const builtin = sources.data?.builtin ?? []
  const plugins = sources.data?.plugins ?? []
  const custom = sources.data?.custom ?? []
  const items = [
    ...builtin.map(s => ({ ...s, kind: 'builtin' as const })),
    ...plugins.map(s => ({ ...s, kind: 'plugin' as const })),
    ...custom.map(s => ({ ...s, kind: 'custom' as const })),
  ]
  const byName = new Map(items.map(s => [s.name, s]))

  const activeName = prefs.data?.daily_data_provider || 'tickflow'
  const selected = picked ?? activeName

  // 切换数据源 —— 与设置页同一套偏好接口:
  // 内置源 5 个数据集全量切换; 其他源仅切换其声明支持的数据集, 其余回落内置源
  const switchProvider = useMutation({
    mutationFn: (name: string) => {
      if (name === 'tickflow') {
        return api.updateDataProviders({
          daily_data_provider: 'tickflow',
          adj_factor_provider: 'same_as_daily',
          realtime_data_provider: 'tickflow',
          minute_data_provider: 'tickflow',
          financial_data_provider: 'tickflow',
        })
      }
      const supported = new Set(byName.get(name)?.datasets ?? [])
      const pick = (dataset: string) => (supported.has(dataset) ? name : 'tickflow')
      return api.updateDataProviders({
        daily_data_provider: pick('daily'),
        adj_factor_provider: 'same_as_daily',
        realtime_data_provider: pick('realtime'),
        minute_data_provider: pick('minute'),
        financial_data_provider: pick('financial'),
      })
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: QK.preferences }),
  })

  const choose = (name: string, available: boolean) => {
    if (!available || name === selected || switchProvider.isPending) return
    setAdding(false)
    setPicked(name)
    switchProvider.mutate(name)
  }

  return (
    <div>
      <div className="flex items-center gap-2.5">
        <div className="rounded-lg bg-accent/10 p-2">
          <Database className="h-4 w-4 text-accent" />
        </div>
        <h2 className="text-xl font-bold text-foreground">配置数据源</h2>
      </div>
      <p className="mt-2.5 text-sm text-secondary leading-relaxed">
        所有数据源均为第三方服务,按需选择或添加自有接口;随时可在
        <span className="text-foreground font-medium"> 设置 → 数据源 </span>
        中调整。
      </p>

      {/* 数据源卡片选择器 */}
      {sources.isLoading ? (
        <div className="mt-4 flex items-center gap-2 text-xs text-muted">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          正在加载数据源…
        </div>
      ) : (
        <div className="mt-4 grid grid-cols-1 sm:grid-cols-2 gap-2.5">
          {items.map(item => {
            const isSelected = selected === item.name
            const unavailable = item.kind === 'plugin' && !item.available
            const plugin = item.kind === 'plugin' ? plugins.find(p => p.name === item.name) : undefined
            // 切换中的目标卡片: 圆点位置显示转圈, 其余卡片压暗
            const switchingToThis = switchProvider.isPending && switchProvider.variables === item.name
            return (
              <button
                key={item.name}
                type="button"
                onClick={() => choose(item.name, !unavailable)}
                disabled={unavailable || switchProvider.isPending}
                title={unavailable ? plugin?.status || '依赖未安装' : item.display_name}
                className={`relative text-left rounded-card border px-3.5 py-3 transition-all ${
                  unavailable
                    ? 'border-border/40 bg-elevated/10 opacity-60 cursor-not-allowed'
                    : isSelected
                      ? 'border-accent/50 bg-accent/[0.06] ring-1 ring-accent/20 cursor-pointer'
                      : 'border-border/60 bg-elevated/20 hover:bg-elevated/40 cursor-pointer'
                } ${switchProvider.isPending && !switchingToThis ? 'opacity-50' : ''}`}
              >
                <div className="flex items-center gap-2">
                  {switchingToThis ? (
                    <Loader2 className="h-3 w-3 shrink-0 animate-spin text-accent" />
                  ) : (
                    <span className={`h-1.5 w-1.5 rounded-full shrink-0 ${
                      isSelected ? 'bg-accent' : 'bg-transparent border border-muted/40'
                    }`} />
                  )}
                  <span className={`text-sm truncate flex-1 ${isSelected ? 'font-medium text-foreground' : 'text-secondary'}`}>
                    {/* 卡片展示去掉声明里的括号备注 (如合规提示), 保持名称干净 */}
                    {item.display_name.replace(/（.*?）|\(.*?\)/g, '').trim() || item.display_name}
                  </span>
                  {item.kind === 'custom' ? (
                    <span className="text-[9px] text-muted/50 tracking-wider shrink-0">自有</span>
                  ) : (
                    <span className="shrink-0 rounded bg-warning/15 px-1 py-0.5 text-[9px] font-medium leading-none text-warning">第三方</span>
                  )}
                </div>
                <div className="mt-1.5 flex items-center gap-1 pl-3.5">
                  {unavailable ? (
                    <span className="text-[10px] text-muted">需安装依赖,见 设置 → 数据源</span>
                  ) : item.datasets.length > 0 ? (
                    item.datasets.slice(0, 4).map(ds => (
                      <span key={ds} className="rounded bg-elevated/60 px-1 py-0.5 text-[10px] text-muted">
                        {DATASET_LABELS[ds] ?? ds}
                      </span>
                    ))
                  ) : (
                    <span className="text-[10px] text-muted">未声明数据集</span>
                  )}
                </div>
              </button>
            )
          })}

          {/* 添加自有数据源卡片 */}
          <button
            type="button"
            onClick={() => setAdding(v => !v)}
            className={`rounded-card border border-dashed px-3.5 py-3 transition-all flex items-center justify-center gap-1.5 text-sm ${
              adding
                ? 'border-accent/50 bg-accent/5 text-accent'
                : 'border-border/50 text-muted hover:text-foreground hover:border-border hover:bg-elevated/30'
            }`}
          >
            <Plus className="h-3.5 w-3.5" />
            添加自有数据源
          </button>
        </div>
      )}

      {/* 插件化提示: 标识数据源体系已插件化, 并给出两条接入路径与文档指引 */}
      <div className="mt-3 flex items-start gap-2 rounded-card border border-border/60 bg-surface/60 px-3 py-2.5">
        <Puzzle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent/70" />
        <div className="text-[11px] leading-relaxed text-muted">
          <span className="text-secondary">数据源已插件化</span>
          ,接入自有行情有两条路径:用 YAML 描述自有 HTTP 接口,放入
          <span className="mx-0.5 rounded bg-elevated/70 px-1 py-px font-mono text-[10px] text-secondary">data/data_sources/*.yaml</span>
          (也可用上方表单配置);或开发插件源,放入
          <span className="mx-0.5 rounded bg-elevated/70 px-1 py-px font-mono text-[10px] text-secondary">backend/app/plugins/</span>
          目录。接入方法与字段映射详见
          <span className="mx-0.5 rounded bg-elevated/70 px-1 py-px font-mono text-[10px] text-secondary">docs/custom-data-source.md</span>
          与
          <span className="mx-0.5 rounded bg-elevated/70 px-1 py-px font-mono text-[10px] text-secondary">docs/plugin-development.md</span>
          。
        </div>
      </div>

      {switchProvider.isError && (
        <div className="mt-3 flex items-start gap-1.5 rounded-btn border border-danger/30 bg-danger/10 px-3 py-2 text-[11px] leading-snug text-danger">
          <AlertCircle className="h-3.5 w-3.5 mt-px shrink-0" />
          <span>数据源切换失败:{String((switchProvider.error as any)?.message ?? '')}</span>
        </div>
      )}

      {/* 添加自有数据源: 复用设置页编辑器 (命名/鉴权/数据集字段映射/测试/启用) */}
      <AnimatePresence mode="wait">
        {adding && (
          <motion.div
            key="ds-editor"
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            transition={{ duration: 0.15 }}
            className="mt-4"
          >
            <DataSourceEditor
              initial={null}
              onCancel={() => setAdding(false)}
              onSaved={() => {
                qc.invalidateQueries({ queryKey: QK.dataSources })
                setAdding(false)
              }}
              activeName={activeName}
              onActivate={name => switchProvider.mutate(name)}
            />
          </motion.div>
        )}
      </AnimatePresence>

      {/* 底部操作 */}
      <div className="mt-6 flex items-center justify-between">
        <button
          onClick={onBack}
          className="inline-flex items-center gap-1.5 px-3 h-9 rounded-btn text-sm text-secondary hover:text-foreground transition-colors"
        >
          <ArrowLeft className="h-4 w-4" />
          上一步
        </button>
        <button
          onClick={onNext}
          className="inline-flex items-center gap-2 px-5 h-9 rounded-xl bg-accent text-white text-sm font-semibold hover:bg-accent/90 transition-colors"
        >
          下一步
          <ArrowRight className="h-4 w-4" />
        </button>
      </div>
    </div>
  )
}

// ===== Step 3: 能力探测结果 =====
// 按当前实际选中的数据源分流:
// - TickFlow: 提示第三方源性质 + 可配 Key、按 Key 匹配档位, 展示档位与能力探测
// - 其他源: 弱化 TickFlow (不展示其档位/能力), 只汇总所选源的数据集覆盖与回落规则

function ResultStep({ onNext, onBack }: { onNext: () => void; onBack: () => void }) {
  const settings = useSettings()
  const caps = useCapabilities()
  const prefs = usePreferences()
  const sources = useQuery({
    queryKey: QK.dataSources,
    queryFn: api.dataSources,
    staleTime: 60_000,
  })

  const activeName = prefs.data?.daily_data_provider || 'tickflow'
  const isTickflow = activeName === 'tickflow'
  const sourceItem = [
    ...(sources.data?.builtin ?? []),
    ...(sources.data?.plugins ?? []),
    ...(sources.data?.custom ?? []),
  ].find(s => s.name === activeName)

  // 是否配置成功 —— 免费档(free)或付费档(api_key)都算;None 档算未配置
  const hasKey = settings.data?.mode === 'free' || settings.data?.mode === 'api_key'
  const capList = caps.data ? Object.entries(caps.data.capabilities) : []

  return (
    <div>
      <div className="flex items-center gap-2.5">
        <div className="rounded-lg bg-accent/10 p-2">
          <ScanSearch className="h-4 w-4 text-accent" />
        </div>
        <h2 className="text-xl font-bold text-foreground">能力探测结果</h2>
      </div>

      {prefs.isLoading ? (
        <div className="mt-5 flex items-center gap-2 text-xs text-muted">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          正在读取当前数据源…
        </div>
      ) : isTickflow ? (
        <>
          {/* TickFlow 源说明: 点明第三方性质 + Key/档位关系 */}
          <div className="mt-4 flex items-start gap-2.5 rounded-card border border-border/60 bg-surface/60 px-3.5 py-3">
            <Database className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent/70" />
            <div className="text-[11px] leading-relaxed text-muted">
              <span className="text-secondary">当前选择了 TickFlow 第三方数据源</span>
              。实时行情、监控等能力与订阅档位由 TickFlow Key 决定:可在
              <span className="text-foreground font-medium"> 设置 → 账户 </span>
              配置 Key,系统会根据 Key 自动匹配档位;未配置 Key 时按 None 档运行,仅保留内置历史数据能力。
            </div>
          </div>

          {hasKey ? (
            <>
              <p className="mt-2.5 text-sm text-secondary leading-relaxed">
                Key 已生效,以下是你当前可用的全部能力。后续可在
                <span className="text-foreground font-medium"> 设置 → 账户 </span>
                中重新检测或更换 Key。
              </p>

              <div className="mt-5 rounded-card border border-border bg-surface/80 backdrop-blur-sm p-5">
                <div className="flex items-baseline justify-between">
                  <span className="text-[10px] uppercase tracking-widest text-muted">订阅档位</span>
                  <span className="font-mono text-2xl font-bold tracking-tight text-foreground">
                    {caps.data?.label ?? settings.data?.tier_label ?? '—'}
                  </span>
                </div>

                {caps.isLoading ? (
                  <div className="mt-4 flex items-center gap-2 text-xs text-muted">
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    正在探测能力…
                  </div>
                ) : capList.length > 0 ? (
                  <div className="mt-4 grid grid-cols-1 gap-1.5">
                    {capList.slice(0, 8).map(([cap]) => {
                      const meta = CAP_LABELS[cap]
                      return (
                        <div key={cap} className="flex items-center gap-2 text-xs">
                          <CheckCircle2 className="h-3.5 w-3.5 text-bear shrink-0" />
                          <span className="text-foreground">{meta?.name ?? cap}</span>
                        </div>
                      )
                    })}
                    {capList.length > 8 && (
                      <div className="text-[11px] text-muted pl-5">…等共 {capList.length} 项</div>
                    )}
                  </div>
                ) : (
                  <div className="mt-4 text-xs text-muted">暂未探测到能力</div>
                )}
              </div>
            </>
          ) : (
            <div className="mt-5 rounded-card border border-border bg-surface/80 backdrop-blur-sm p-6 text-center">
              <div className="mx-auto w-fit rounded-xl bg-elevated p-3">
                <Zap className="h-6 w-6 text-warning" />
              </div>
              <div className="mt-3 text-sm font-medium text-foreground">将以 None 档继续</div>
              <p className="mt-2 text-xs text-muted leading-relaxed max-w-sm mx-auto">
                当前未配置有效 Key,仍可使用看板、选股、回测等功能 —— 进入看板后可直接获取近 1 年历史日K数据。配置 Key 后可解锁实时行情监控等能力,随时在
                <span className="text-foreground font-medium"> 设置 → 账户 </span>填写。
              </p>
            </div>
          )}
        </>
      ) : (
        /* 其他数据源: 不展示 TickFlow 档位/能力探测, 汇总所选源的数据集覆盖 */
        <div className="mt-5 rounded-card border border-border bg-surface/80 backdrop-blur-sm p-5">
          <div className="flex items-baseline justify-between gap-2">
            <span className="text-[10px] uppercase tracking-widest text-muted">当前数据源</span>
            <span className="flex min-w-0 items-baseline gap-1.5">
              <span className="truncate text-lg font-bold text-foreground">
                {(sourceItem?.display_name ?? activeName).replace(/（.*?）|\(.*?\)/g, '').trim() || sourceItem?.display_name || activeName}
              </span>
              <span className="shrink-0 rounded bg-warning/15 px-1 py-0.5 text-[9px] font-medium leading-none text-warning">
                {sources.data?.custom?.some(s => s.name === activeName) ? '自有' : '第三方'}
              </span>
            </span>
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-1">
            {(sourceItem?.datasets?.length ?? 0) > 0 ? (
              sourceItem!.datasets.map(ds => (
                <span key={ds} className="rounded bg-elevated/60 px-1.5 py-0.5 text-[10px] text-secondary">
                  {DATASET_LABELS[ds] ?? ds}
                </span>
              ))
            ) : (
              <span className="text-[10px] text-muted">未声明数据集</span>
            )}
          </div>
          <p className="mt-2.5 text-xs text-muted leading-relaxed">
            行情能力由所选数据源决定:以上数据集由该源提供,未覆盖的数据集自动回落内置源,无需额外配置。
          </p>

          <div className="mt-3 border-t border-border/60 pt-2.5 text-[11px] leading-relaxed text-muted">
            TickFlow 的 Key 与档位探测仅在选择 TickFlow 作为数据源时展示;如需切换,前往
            <span className="text-foreground font-medium"> 设置 → 数据源 </span>。
          </div>
        </div>
      )}

      {/* 底部操作 */}
      <div className="mt-6 flex items-center justify-between">
        <button
          onClick={onBack}
          className="inline-flex items-center gap-1.5 px-3 h-9 rounded-btn text-sm text-secondary hover:text-foreground transition-colors"
        >
          <ArrowLeft className="h-4 w-4" />
          上一步
        </button>
        <button
          onClick={onNext}
          className="inline-flex items-center gap-2 px-5 h-9 rounded-xl bg-accent text-white text-sm font-semibold hover:bg-accent/90 transition-colors"
        >
          下一步
          <ArrowRight className="h-4 w-4" />
        </button>
      </div>
    </div>
  )
}

// ===== Step 4: 完成 =====

function FinishStep({ onNext, onBack, pending }: { onNext: () => void; onBack: () => void; pending: boolean }) {
  const settings = useSettings()
  // 是否已配置 Key(free 或 api_key 都算,None 档算未配置)
  const hasKey = settings.data?.mode === 'free' || settings.data?.mode === 'api_key'

  // 首要行动:获取数据(不管配没配 Key, 新用户都需要先拉数据)
  // 快速上手入口(精简为核心功能)
  const tips = [
    { icon: TrendingUp, text: '「个股分析」:输入代码,AI 四维分析 + 关键价位' },
    { icon: ScanSearch, text: '「选股」页:内置多套策略,一键扫描全市场' },
    { icon: ShieldCheck, text: '「回测」页:用历史数据验证策略表现,用数据说话' },
  ]

  return (
    <div className="text-center">
      <motion.div
        initial={{ scale: 0.85, opacity: 0 }}
        animate={{ scale: 1, opacity: 1 }}
        transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
        className="mx-auto w-fit"
      >
        <div
          className="relative rounded-2xl p-5 border border-border"
          style={{ background: `linear-gradient(135deg, ${BRAND}22, transparent)` }}
        >
          <CheckCircle2 className="h-12 w-12 text-bear" />
          {/* 光晕脉冲 */}
          <motion.div
            animate={{ scale: [1, 1.4], opacity: [0.4, 0] }}
            transition={{ duration: 1.8, repeat: Infinity, ease: 'easeOut' }}
            className="absolute inset-5 rounded-full bg-bear/30"
          />
        </div>
      </motion.div>

      <h1 className="mt-6 text-2xl font-bold text-foreground">一切就绪!</h1>
      <p className="mt-2.5 text-sm text-secondary leading-relaxed max-w-md mx-auto">
        {hasKey
          ? 'Key 已生效,进入面板后系统会自动引导你获取行情数据,完成后即可使用全部功能。'
          : '当前为 None 档,进入面板后系统会自动引导你获取历史日K数据(无需 Key),即可开始体验。'}
      </p>

      {/* 首要行动:获取数据 */}
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.3, delay: 0.2 }}
        className="mt-5 flex items-start gap-2.5 rounded-card border border-accent/30 bg-accent/[0.06] px-4 py-3 text-left"
      >
        <div className="rounded-lg bg-accent/15 p-1.5 shrink-0 mt-px">
          <Database className="h-4 w-4 text-accent" />
        </div>
        <div className="min-w-0">
          <div className="text-sm font-medium text-foreground">下一步:获取行情数据</div>
          <p className="mt-1 text-xs text-secondary leading-relaxed">
            进入面板后,看板会自动引导你拉取近 1 年全 A 股日K(约 5500 只,预计 1-3 分钟)。同步期间可浏览其他页面。
          </p>
        </div>
      </motion.div>

      {/* 快速上手入口 */}
      <div className="mt-4 space-y-2 text-left">
        {tips.map((t, i) => (
          <motion.div
            key={i}
            initial={{ opacity: 0, x: -10 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ duration: 0.3, delay: 0.1 * i + 0.3 }}
            className="flex items-center gap-3 rounded-card border border-border bg-surface/80 backdrop-blur-sm px-3.5 py-2.5"
          >
            <div className="rounded-lg bg-accent/10 p-1.5 shrink-0">
              <t.icon className="h-3.5 w-3.5 text-accent" />
            </div>
            <span className="text-xs text-secondary">{t.text}</span>
          </motion.div>
        ))}
      </div>

      {/* 底部操作 */}
      <div className="mt-8 flex items-center justify-between">
        <button
          onClick={onBack}
          className="inline-flex items-center gap-1.5 px-3 h-10 rounded-btn text-sm text-secondary hover:text-foreground transition-colors"
        >
          <ArrowLeft className="h-4 w-4" />
          上一步
        </button>
        <button
          onClick={onNext}
          disabled={pending}
          className="inline-flex items-center gap-2 px-6 h-10 rounded-xl bg-accent text-white text-sm font-semibold shadow-lg shadow-accent/20 hover:bg-accent/90 hover:shadow-accent/30 disabled:opacity-60 transition-all"
        >
          {pending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
          {pending ? '正在进入…' : '进入面板'}
        </button>
      </div>
    </div>
  )
}
