import { useEffect, useMemo, useRef, useState } from 'react'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'
import { isCryptoAssetType, type MinuteKlineRow, type PriceLimitInfo } from '@/lib/api'
import {
  computeIntradayAverage,
  formatIntradayVolume,
  formatMinuteTime,
  FULL_DAY_TIMES,
  intradayAxisTimes,
  intradayPriceDigits,
  summarizeMinutes,
  type DailySummary,
} from '@/lib/intraday-chart'
import { useChartTheme, type ChartTheme } from '@/lib/theme'

type YMode = 'adaptive' | 'limit'

// 序列颜色 (双主题通用); 画布轴/网格/十字线等主题相关色走 ChartTheme
const THEME = {
  line: '#3B82F6',
  areaFill: 'rgba(59,130,246,0.40)',
  avgLine: '#F59E0B',
  volUp: 'rgba(240,68,56,0.6)',
  volDown: 'rgba(18,183,106,0.6)',
}

interface Props {
  data: MinuteKlineRow[]
  height?: number
  prevClose?: number
  date?: string
  dailySummary?: DailySummary
  /** 资产类别; crypto 走数据驱动时轴与自适应 Y 轴 (无涨跌停), 缺省按 A 股/ETF/指数处理。 */
  assetType?: string
  priceLimit?: PriceLimitInfo
  onPriceHover?: (price: number | null) => void
  onPriceDoubleClick?: (price: number, currentPrice: number) => void
  currentPrice?: number
  priceLines?: { value: number; label?: string; color?: string }[]
  showLimitLines?: boolean
  showAvgLine?: boolean
}

function fmtAmt(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '—'
  if (v >= 1_000_000_000) return `${(v / 1_000_000_000).toFixed(2)}亿`
  if (v >= 10_000) return `${(v / 10_000).toFixed(0)}万`
  return v.toFixed(0)
}

function isValidPrice(v: number | null | undefined): v is number {
  return typeof v === 'number' && Number.isFinite(v) && v > 0
}

function axisIndexFromValue(value: unknown, axisTimes: string[]): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return Math.round(value)
  }
  if (typeof value === 'string') {
    const idx = axisTimes.indexOf(value)
    return idx >= 0 ? idx : null
  }
  return null
}

/** 计算实际涨跌停价 (四舍五入到2位小数) 和实际涨跌停幅度 */
function getLimitPrices(prevClose: number, priceLimit?: PriceLimitInfo): {
  limitUp: number      // 涨停价 (四舍五入)
  limitDown: number    // 跌停价 (四舍五入)
  upPct: number        // 实际涨停幅度 (如 9.97)
  downPct: number      // 实际跌停幅度 (如 -9.97)
} {
  const pct = priceLimit && Number.isFinite(priceLimit.rate) ? priceLimit.rate : 0.10
  const rawUp = prevClose * (1 + pct)
  const rawDown = prevClose * (1 - pct)
  // A股涨跌停价四舍五入到分 (2位小数)
  const limitUp = isValidPrice(priceLimit?.limit_up)
    ? priceLimit.limit_up
    : Math.round(rawUp * 100) / 100
  const limitDown = isValidPrice(priceLimit?.limit_down)
    ? priceLimit.limit_down
    : Math.round(rawDown * 100) / 100
  const upPct = (limitUp - prevClose) / prevClose * 100
  const downPct = (limitDown - prevClose) / prevClose * 100
  return { limitUp, limitDown, upPct, downPct }
}

function buildOption(data: MinuteKlineRow[], prevClose: number | undefined, avgPrices: (number | null)[], lineColor: string, areaColor: string, yMode: YMode, ct: ChartTheme, assetType?: string, priceLimit?: PriceLimitInfo, showLimitLines = true, showAvgLine = true, priceLines: Props['priceLines'] = []): EChartsOption {
  const isCrypto = isCryptoAssetType(assetType)
  // 无涨跌幅标的: crypto (后端 price_limit=null) 与注册制新股上市初期窗口 (后端
  // no_limit 标记) 都不存在可信涨跌停带, 自适应/涨跌停两类模式都退化为纯数据对称
  // 范围, 也不画涨跌停虚线
  const limitLinesActive = showLimitLines && !isCrypto && !priceLimit?.no_limit
  const priceDigits = intradayPriceDigits(assetType, data.map(row => row.high))
  // crypto 时轴 = 数据自身时间点 (行序即轴序), A 股/ETF/指数 = 固定全天 242 槽
  const axisTimes = intradayAxisTimes(assetType, data)
  const timeIndexMap = isCrypto ? null : new Map(FULL_DAY_TIMES.map((t, i) => [t, i]))
  const closes = new Array(axisTimes.length).fill(null) as (number | null)[]
  const highs = new Array(axisTimes.length).fill(null) as (number | null)[]
  const lows = new Array(axisTimes.length).fill(null) as (number | null)[]
  const avgData = new Array(axisTimes.length).fill(null) as (number | null)[]
  const volumes = new Array(axisTimes.length).fill(null) as (any | null)[]

  const volNeutral = 'rgba(161,161,170,0.5)'
  // 量柱着色基准: 前一分钟 close; 第一根用昨收。
  // 不用 row.open — 部分分钟源历史日无真实 open(为 null), close-vs-open 会全偏。
  let prevRef: number | null = prevClose ?? null
  for (let i = 0; i < data.length; i++) {
    // 时轴槽位外的分钟只对 A 股/ETF/指数存在 (午休/非交易时段); crypto 数据自身即时轴, 不会丢
    const idx = timeIndexMap === null ? i : timeIndexMap.get(formatMinuteTime(data[i].datetime))
    if (idx !== undefined) {
      closes[idx] = data[i].close
      highs[idx] = data[i].high
      lows[idx] = data[i].low
      avgData[idx] = avgPrices[i]
      volumes[idx] = {
        value: data[i].volume,
        itemStyle: {
          color: prevRef == null
            ? volNeutral
            : data[i].close > prevRef
              ? THEME.volUp
              : data[i].close < prevRef
                ? THEME.volDown
                : volNeutral,
        },
      }
      prevRef = data[i].close
    }
  }

  const areaStyle: any = {
    color: {
      type: 'linear',
      x: 0, y: 0, x2: 0, y2: 1,
      colorStops: [
        { offset: 0, color: areaColor },
        { offset: 1, color: 'rgba(0,0,0,0)' },
      ],
    },
  }

  const markLineData: any[] = []
  if (prevClose != null) {
    markLineData.push({
      yAxis: prevClose,
      lineStyle: { color: ct.crosshair, type: 'dashed', width: 1 },
      label: { show: false },
      symbol: 'none',
    })
  }
  for (const line of priceLines) {
    if (!Number.isFinite(line.value) || line.value <= 0) continue
    markLineData.push({
      yAxis: line.value,
      lineStyle: { color: line.color ?? ct.text, type: 'dashed', width: 1, opacity: 0.92 },
      label: {
        show: !!line.label,
        formatter: line.label ?? '',
        position: 'insideEndTop',
        color: line.color ?? ct.text,
        backgroundColor: ct.tooltipBg,
        borderRadius: 4,
        padding: [2, 6],
        fontSize: 10,
        fontFamily: 'JetBrains Mono, monospace',
      },
      symbol: 'none',
    })
  }

  let yMin: number | undefined
  let yMax: number | undefined
  let maxDiff = 0
  if (isValidPrice(prevClose) && data.length > 0) {
    const priceArrays = showAvgLine ? [closes, highs, lows, avgData] : [closes, highs, lows]
    for (const arr of priceArrays) {
      for (const v of arr) {
        if (!isValidPrice(v)) continue
        const diff = Math.abs(v - prevClose)
        if (diff > maxDiff) maxDiff = diff
      }
    }

    const monitoredDiff = priceLines.reduce((largest, line) => (
      Number.isFinite(line.value) && line.value > 0
        ? Math.max(largest, Math.abs(line.value - prevClose))
        : largest
    ), 0) * 1.05

    if (limitLinesActive && yMode === 'limit') {
      const { limitUp, limitDown } = getLimitPrices(prevClose, priceLimit)
      const limitDiffUp = limitUp - prevClose
      const limitDiffDown = prevClose - limitDown
      const limitDiff = Math.max(limitDiffUp, limitDiffDown)
      // 涨跌停模式: Y 轴按实际涨跌停价, 但不小于实际数据范围 (数据超带 =
      // 涨跌幅规则不适用或数据异常, 钳制会把曲线推出图外)
      maxDiff = Math.max(limitDiff, maxDiff, monitoredDiff)
      yMin = prevClose - maxDiff
      yMax = prevClose + maxDiff
      // 加 markLine 标注涨停价和跌停价 (仅虚线, 不显示文字)
      markLineData.push(
        {
          yAxis: limitUp,
          lineStyle: { color: 'rgba(199,64,64,0.4)', type: 'dashed', width: 1 },
          label: { show: false },
          symbol: 'none',
        },
        {
          yAxis: limitDown,
          lineStyle: { color: 'rgba(45,155,101,0.4)', type: 'dashed', width: 1 },
          label: { show: false },
          symbol: 'none',
        },
      )
    } else {
      // 自适应模式: Y 轴按实际涨跌幅对称。不做涨跌停带上限钳制 — 带内数据
      // maxDiff 本就 ≤ limitDiff (钳制是恒等), 而数据超带时 (无涨跌幅新股/
      // 数据异常) 钳制会把曲线推出图外 (issue: C沈鼓 分时被夹在 ±10% 内)
      if (!limitLinesActive && maxDiff > 0) {
        maxDiff *= 1.1
      }
      // 至少保证一个可视范围 (防止数据平时 maxDiff=0)。指数不使用涨跌停范围，最小范围要更紧，否则低波动指数会被压成横线。
      const minDiff = limitLinesActive ? prevClose * 0.01 : prevClose * 0.001
      if (maxDiff < minDiff) maxDiff = minDiff
      maxDiff = Math.max(maxDiff, monitoredDiff)
      yMin = prevClose - maxDiff
      yMax = prevClose + maxDiff
    }
  }

  // x 轴标签: 9:30, 10:30, 11:30/13:00, 14:00, 15:00
  // 11:30(idx 120) 和 13:00(idx 121) 相邻会重叠, 合并为一个标签。
  // 该槽位映射只对固定 242 槽的 A 股/ETF/指数成立; crypto 无固定时段,
  // 按数据长度均匀取约 6 个时间标签, 直接用时轴上的真实时间。
  const xAxisLabelMap: Record<number, string> = {
    0: '9:30',
    60: '10:30',
    120: '11:30/13:00',
    181: '14:00',
    241: '15:00',
  }
  const cryptoLabelStep = Math.max(1, Math.ceil(axisTimes.length / 6))
  const xAxisLabelFormatter = (_value: string, idx: number) => {
    if (isCrypto) {
      return idx % cryptoLabelStep === 0 ? axisTimes[idx] : ''
    }
    return xAxisLabelMap[idx] ?? ''
  }

  return {
    animation: false,
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'transparent',
      borderWidth: 0,
      textStyle: { fontSize: 0 },
      formatter: () => '',
      axisPointer: {
        type: 'cross',
        label: {
          show: true,
          backgroundColor: ct.tooltipBg,
          borderColor: ct.tooltipBorder,
          borderWidth: 1,
          padding: [2, 5],
          color: ct.tooltipText,
          fontSize: 10,
          fontFamily: 'JetBrains Mono, monospace',
        },
        crossStyle: { color: ct.crosshair, type: 'dashed', width: 1 },
        lineStyle: { color: ct.crosshair, type: 'dashed', width: 1 },
      },
    },
    axisPointer: {
      link: [{ xAxisIndex: 'all' }],
    },
    grid: [
      { left: 60, right: 55, top: 24, bottom: '34%' },
      { left: 60, right: 55, top: '69%', bottom: 20 },
    ],
    xAxis: [
      {
        type: 'category',
        data: axisTimes,
        boundaryGap: false,
        axisPointer: {
          show: true,
          lineStyle: { color: ct.crosshair, type: 'dashed', width: 1 },
          label: {
            show: true,
            backgroundColor: ct.tooltipBg,
            borderColor: ct.tooltipBorder,
            borderWidth: 1,
            padding: [2, 4],
            color: ct.tooltipText,
            fontSize: 10,
            fontFamily: 'JetBrains Mono, monospace',
            formatter: (params: any) => {
              return params.value ?? ''
            },
          },
        },
        axisLine: { show: false },
        axisLabel: {
          color: ct.text,
          fontSize: 10,
          fontFamily: 'JetBrains Mono, monospace',
          formatter: xAxisLabelFormatter,
          interval: 0,
        },
        axisTick: { show: false },
        splitLine: {
          show: true,
          lineStyle: { color: ct.grid },
        },
      },
      {
        type: 'category',
        gridIndex: 1,
        data: axisTimes,
        boundaryGap: false,
        axisLine: { show: false },
        axisLabel: { show: false },
        axisTick: { show: false },
        splitLine: { show: false },
      },
    ],
    yAxis: [
      {
        type: 'value',
        min: yMin,
        max: yMax,
        // 上市首日无 prevClose 时 min/max 为 undefined, scale 避免 value 轴
        // 默认从 0 锚定把曲线挤到图上部 (有 min/max 时本属性无影响)。
        // 区间过宽时 (无涨跌幅新股, maxDiff 远超昨收) interval 减半让昨收
        // 落在刻度上; 正常涨跌停带内保持原 interval 行为
        scale: true,
        interval: prevClose != null && maxDiff > prevClose * 0.5 ? maxDiff / 2 : maxDiff || undefined,
        splitArea: { show: false },
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { lineStyle: { color: ct.grid } },
        axisPointer: {
          label: {
            formatter: (params: any) => {
              const v = params.value
              return typeof v === 'number' ? v.toFixed(priceDigits) : ''
            },
          },
        },
        axisLabel: {
          color: ct.text,
          fontSize: 10,
          fontFamily: 'JetBrains Mono, monospace',
          formatter: (v: number) => v.toFixed(priceDigits),
        },
      },
      {
        scale: true,
        gridIndex: 1,
        splitNumber: 2,
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { show: false },
        axisLabel: { show: false },
      },
      ...(isValidPrice(prevClose) && yMin != null && yMax != null ? [{
        type: 'value' as const,
        position: 'right' as const,
        gridIndex: 0,
        min: yMin,
        max: yMax,
        interval: maxDiff || undefined,
        splitArea: { show: false },
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { show: false },
        axisPointer: {
          label: {
            formatter: (params: any) => {
              const v = params.value
              if (typeof v !== 'number') return ''
              const pct = (v - prevClose) / prevClose * 100
              if (Math.abs(pct) < 0.01) return '0.00%'
              return (pct > 0 ? '+' : '') + pct.toFixed(2) + '%'
            },
          },
        },
        axisLabel: {
          color: ct.text,
          fontSize: 10,
          fontFamily: 'JetBrains Mono, monospace',
          formatter: (v: number) => {
            const pct = (v - prevClose) / prevClose * 100
            if (Math.abs(pct) < 0.01) return '0.00%'
            return (pct > 0 ? '+' : '') + pct.toFixed(2) + '%'
          },
        },
      }] : []),
    ],
    series: [
      {
        name: '价格',
        type: 'line',
        data: closes,
        smooth: false,
        symbol: 'none',
        cursor: 'crosshair',
        lineStyle: { width: 1.2, color: lineColor },
        areaStyle,
        connectNulls: true,
        markLine: markLineData.length > 0 ? { symbol: 'none', data: markLineData, animation: false, silent: true } : undefined,
      },
      ...(showAvgLine ? [{
        name: '均价',
        type: 'line' as const,
        data: avgData,
        smooth: false,
        symbol: 'none',
        cursor: 'crosshair',
        lineStyle: { width: 1, color: THEME.avgLine },
        connectNulls: true,
      }] : []),
      {
        name: '成交量',
        type: 'bar',
        data: volumes,
        xAxisIndex: 1,
        yAxisIndex: 1,
        cursor: 'crosshair',
      },
    ],
  }
}

export function EChartsIntraday({
  data,
  height = 320,
  prevClose,
  date,
  dailySummary,
  assetType,
  priceLimit,
  onPriceHover,
  onPriceDoubleClick,
  currentPrice,
  priceLines,
  showLimitLines = true,
  showAvgLine = true,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<ECharts | null>(null)
  const roRef = useRef<ResizeObserver | null>(null)
  const moRef = useRef<MutationObserver | null>(null)
  const chartClickHandlerRef = useRef<((event: MouseEvent) => void) | null>(null)
  const clickLockTimerRef = useRef<number | null>(null)
  const priceDoubleClickHandlerRef = useRef<((event: { offsetX: number; offsetY: number }) => void) | null>(null)
  const dataRef = useRef(data)
  dataRef.current = data
  const currentPriceRef = useRef(currentPrice)
  currentPriceRef.current = currentPrice
  const onPriceHoverRef = useRef(onPriceHover)
  onPriceHoverRef.current = onPriceHover
  const onPriceDoubleClickRef = useRef(onPriceDoubleClick)
  onPriceDoubleClickRef.current = onPriceDoubleClick
  // 时轴索引 → 数据数组索引 的映射 (ref 避免重建 chart)
  const axisIdxToDataIdx = useRef<Map<number, number>>(new Map())
  const lockedInfoIdxRef = useRef<number | null>(null)
  const infoIdxRef = useRef(data.length - 1)

  const [infoIdx, setInfoIdx] = useState(-1)
  const [yMode, setYMode] = useState<YMode>('adaptive')
  const ct = useChartTheme()
  const summary = useMemo(() => summarizeMinutes(data), [data])
  const isCrypto = isCryptoAssetType(assetType)
  // crypto 无涨跌停价 (后端 price_limit=null): 不显示涨跌停/自适应切换; 无涨跌幅
  // 新股仍保留切换 (buildOption 内退化处理), 与 A 股口径一致
  const limitLines = showLimitLines && !isCrypto
  const axisTimes = useMemo(() => intradayAxisTimes(assetType, data), [assetType, data])
  const axisTimesRef = useRef(axisTimes)
  axisTimesRef.current = axisTimes
  const priceDigits = useMemo(() => intradayPriceDigits(assetType, data.map(row => row.high)), [assetType, data])
  const avgPrices = useMemo(() => computeIntradayAverage(data, assetType), [data, assetType])

  // 分时线颜色：基于最新价 vs 昨收
  const lastClose = data.length > 0 ? data[data.length - 1].close : null
  const lineIsUp = lastClose != null && prevClose != null ? lastClose > prevClose : true
  const lineIsFlat = lastClose != null && prevClose != null ? lastClose === prevClose : false
  const lineColor = lineIsFlat ? '#A1A1AA' : lineIsUp ? '#C74040' : '#2D9B65'
  const areaFill = lineIsFlat ? 'rgba(180,180,190,0.40)' : lineIsUp ? 'rgba(199,64,64,0.40)' : 'rgba(34,197,94,0.40)'

  const updateInfoIdx = (next: number) => {
    infoIdxRef.current = next
    setInfoIdx(next)
  }

  useEffect(() => {
    // 换日: 清掉点击锁定的分钟, 回到「无选中」态 (-1 → 信息条显示分时汇总/日K)
    lockedInfoIdxRef.current = null
    setInfoIdx(-1)
  }, [date])

  // 数据更新: 被点击锁定的分钟索引越界时自动解锁, 避免跨日/截断后指向错位
  useEffect(() => {
    const lockedIdx = lockedInfoIdxRef.current
    if (lockedIdx != null && lockedIdx >= data.length) {
      lockedInfoIdxRef.current = null
      updateInfoIdx(-1)
    }
  }, [data.length])

  useEffect(() => {
    const current = infoIdx >= 0 && infoIdx < data.length ? data[infoIdx] : null
    onPriceHover?.(current?.close ?? null)
  }, [data, infoIdx, onPriceHover])

  useEffect(() => {
    const el = containerRef.current
    if (!el) return

    let chart = chartRef.current
    if (!chart) {
      chart = echarts.init(el, undefined, { renderer: 'canvas' })
      chartRef.current = chart
      // 强制 canvas 使用十字光标，覆盖 ECharts 默认的 pointer
      const forceCursor = () => {
        const canvases = el.querySelectorAll('canvas')
        canvases.forEach(c => { c.style.setProperty('cursor', 'crosshair', 'important') })
      }
      forceCursor()
      // MutationObserver: ECharts 内部可能重建/修改 canvas 属性，持续强制 cursor
      const mo = new MutationObserver(forceCursor)
      mo.observe(el, { childList: true, subtree: true, attributes: true, attributeFilter: ['style', 'class'] })
      moRef.current = mo
      roRef.current = new ResizeObserver(() => {
        chart!.resize()
        forceCursor()
      })
      roRef.current.observe(el)

      chart.on('updateAxisPointer', (event: any) => {
        if (lockedInfoIdxRef.current != null) return
        const axesInfo = event.axesInfo
        if (!axesInfo) return
        for (const info of Object.values(axesInfo)) {
          if ((info as any)?.axisDim !== 'x') continue
          const val = (info as any)?.value
          if (val == null) continue
          const axisIdx = axisIndexFromValue(val, axisTimesRef.current)
          if (axisIdx != null && axisIdx >= 0) {
            const dataIdx = axisIdxToDataIdx.current.get(axisIdx) ?? -1
            if (dataIdx >= 0) {
              updateInfoIdx(dataIdx)
            } else {
              // 命中分钟空洞: 回落「无选中」(显示汇总); 不写 ref, 免得点击锁定位错乱
              setInfoIdx(-1)
            }
            return
          }
        }
      })

      chart.on('globalout', () => {
        // 点击锁定期间鼠标移出不移除选中
        if (lockedInfoIdxRef.current != null) return
        updateInfoIdx(-1)
      })

      const handleChartClick = (event: MouseEvent) => {
        if (clickLockTimerRef.current != null) {
          window.clearTimeout(clickLockTimerRef.current)
          clickLockTimerRef.current = null
        }
        const rect = el.getBoundingClientRect()
        const point: [number, number] = [event.clientX - rect.left, event.clientY - rect.top]
        clickLockTimerRef.current = window.setTimeout(() => {
          clickLockTimerRef.current = null
          if (lockedInfoIdxRef.current != null) {
            lockedInfoIdxRef.current = null
            updateInfoIdx(-1)
            return
          }

          const inMainGrid = chart!.containPixel({ gridIndex: 0 }, point)
          const inVolumeGrid = chart!.containPixel({ gridIndex: 1 }, point)
          if (!inMainGrid && !inVolumeGrid) return

          const converted = chart!.convertFromPixel({ xAxisIndex: inVolumeGrid ? 1 : 0 }, point)
          const xValue = Array.isArray(converted) ? converted[0] : converted

          const axisIdx = axisIndexFromValue(xValue, axisTimesRef.current)
          const dataIdx = axisIdx == null ? infoIdxRef.current : axisIdxToDataIdx.current.get(axisIdx)
          // dataIdx < 0 = 无选中 (未命中分钟), 不作为锁定目标
          if (dataIdx == null || dataIdx < 0) return

          lockedInfoIdxRef.current = dataIdx
          updateInfoIdx(dataIdx)
        }, 180)
      }
      chartClickHandlerRef.current = handleChartClick
      el.addEventListener('click', handleChartClick)

      const handlePriceDoubleClick = (event: { offsetX: number; offsetY: number }) => {
        if (clickLockTimerRef.current != null) {
          window.clearTimeout(clickLockTimerRef.current)
          clickLockTimerRef.current = null
        }
        const pixel: [number, number] = [event.offsetX, event.offsetY]
        if (!chart!.containPixel({ gridIndex: 0 }, pixel)) return
        const coordinate = chart!.convertFromPixel({ xAxisIndex: 0, yAxisIndex: 0 }, pixel)
        const clickedPrice = Array.isArray(coordinate) ? Number(coordinate[1]) : NaN
        const latestPrice = currentPriceRef.current ?? dataRef.current[dataRef.current.length - 1]?.close
        if (Number.isFinite(clickedPrice) && clickedPrice > 0 && Number.isFinite(latestPrice) && latestPrice > 0) {
          onPriceDoubleClickRef.current?.(clickedPrice, latestPrice)
        }
      }
      priceDoubleClickHandlerRef.current = handlePriceDoubleClick
      chart.getZr().on('dblclick', handlePriceDoubleClick)
    }

    if (data.length > 0) {
      // 构建时轴索引 → 数据索引 的映射
      const mapping = new Map<number, number>()
      if (isCrypto) {
        // crypto 时轴由数据自身构成, 轴下标 = 数据下标
        for (let i = 0; i < data.length; i++) mapping.set(i, i)
      } else {
        const timeIndexMap = new Map(FULL_DAY_TIMES.map((t, i) => [t, i]))
        for (let i = 0; i < data.length; i++) {
          const timeKey = formatMinuteTime(data[i].datetime)
          const fullDayIdx = timeIndexMap.get(timeKey)
          if (fullDayIdx !== undefined) {
            mapping.set(fullDayIdx, i)
          }
        }
      }
      axisIdxToDataIdx.current = mapping

      chart.setOption(buildOption(data, prevClose, avgPrices, lineColor, areaFill, yMode, ct, assetType, priceLimit, showLimitLines, showAvgLine, priceLines), true)
    } else {
      axisIdxToDataIdx.current = new Map()
      chart.clear()
    }
  }, [data, prevClose, height, lineColor, areaFill, yMode, ct, assetType, isCrypto, priceLimit, showLimitLines, showAvgLine, priceLines])

  useEffect(() => {
    return () => {
      if (clickLockTimerRef.current != null) {
        window.clearTimeout(clickLockTimerRef.current)
        clickLockTimerRef.current = null
      }
      if (containerRef.current && chartClickHandlerRef.current) {
        containerRef.current.removeEventListener('click', chartClickHandlerRef.current)
      }
      chartRef.current?.off('updateAxisPointer')
      chartRef.current?.off('globalout')
      if (priceDoubleClickHandlerRef.current) {
        chartRef.current?.getZr().off('dblclick', priceDoubleClickHandlerRef.current)
      }
      moRef.current?.disconnect()
      roRef.current?.disconnect()
      chartRef.current?.dispose()
      chartRef.current = null
      moRef.current = null
      roRef.current = null
      chartClickHandlerRef.current = null
      priceDoubleClickHandlerRef.current = null
    }
  }, [])

  const hovered = infoIdx >= 0 && infoIdx < data.length ? data[infoIdx] : null
  const d = hovered ?? summary
  const daily = dailySummary?.date === date ? dailySummary : undefined
  const ohlc = hovered ?? daily ?? summary
  const avg = d != null ? avgPrices[hovered ? infoIdx : data.length - 1] : null
  const chg = d && prevClose != null ? d.close - prevClose : null
  const isUp = chg != null ? chg > 0 : true
  const isFlat = chg != null ? chg === 0 : false
  const priceClr = isFlat ? '#A1A1AA' : isUp ? '#C74040' : '#2D9B65'

  return (
    <div className="w-full">
      {/* 按钮行: 切换式按钮组, 居右 (crypto 无涨跌停价, 不显示) */}
      {limitLines && <div className="flex items-center justify-end px-1 pb-0.5">
        <div className="inline-flex items-center rounded bg-elevated overflow-hidden">
          <button
            onClick={() => setYMode('adaptive')}
            className={`px-2.5 py-0.5 text-[10px] font-mono cursor-pointer transition-colors ${
              yMode === 'adaptive'
                ? 'bg-accent/20 text-accent'
                : 'text-muted hover:text-secondary'
            }`}
          >
            自适应
          </button>
          <div className="w-px h-3 bg-border/40" />
          <button
            onClick={() => setYMode('limit')}
            className={`px-2.5 py-0.5 text-[10px] font-mono cursor-pointer transition-colors ${
              yMode === 'limit'
                ? 'bg-accent/20 text-accent'
                : 'text-muted hover:text-secondary'
            }`}
          >
            涨跌停
          </button>
        </div>
      </div>}
      <div style={{ backgroundColor: ct.infoBarBg }}>
        {/* 第一行: 日期 + OHLC */}
        <div className="flex items-center gap-x-2 px-2 font-mono text-[11px] select-none flex-wrap" style={{ minHeight: 20 }}>
          {!d && <span className="text-muted">—</span>}
          {ohlc && (
            <>
              {date && <span className="text-muted">{date}</span>}
              <span className="text-muted">{hovered ? `${formatMinuteTime(hovered.datetime)} 分钟` : daily ? (isCrypto ? '日K' : '日K（前复权）') : '分时汇总'}</span>
              <span className="text-muted">开</span>
              <span style={{ color: priceClr }}>{ohlc.open != null ? ohlc.open.toFixed(priceDigits) : '—'}</span>
              <span className="text-muted">高</span>
              <span style={{ color: priceClr }}>{ohlc.high.toFixed(priceDigits)}</span>
              <span className="text-muted">低</span>
              <span style={{ color: priceClr }}>{ohlc.low.toFixed(priceDigits)}</span>
              <span className="text-muted">收</span>
              <span style={{ color: priceClr }} className="font-semibold">{ohlc.close.toFixed(priceDigits)}</span>
            </>
          )}
        </div>
        {/* 第二行: 价格+均价+量+额 */}
        <div className="flex flex-wrap items-center gap-x-2 px-2 font-mono text-[11px] select-none" style={{ minHeight: 20 }}>
          {d && (
            <>
              <span className="flex items-center gap-x-1">
                <span style={{ display: 'inline-block', width: 14, height: 2, background: priceClr }} />
                <span style={{ color: priceClr }}>{d.close.toFixed(priceDigits)}</span>
              </span>
              {showAvgLine && <span className="flex items-center gap-x-1">
                <span style={{ display: 'inline-block', width: 14, height: 2, background: THEME.avgLine }} />
                <span style={{ color: THEME.avgLine }}>{avg != null ? avg.toFixed(priceDigits) : '—'}</span>
              </span>}
              <span className="text-muted">{hovered ? '量' : '累计量'}</span>
              <span className="text-secondary">{formatIntradayVolume(d.volume, assetType)}</span>
              <span className="text-muted">{hovered ? '额' : '累计额'}</span>
              <span className="text-secondary">{fmtAmt(d.amount)}</span>
            </>
          )}
        </div>
      </div>
      <div ref={containerRef} className="w-full" style={{ height: height - 42, cursor: 'crosshair' }} />
    </div>
  )
}
