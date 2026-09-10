/**
 * 子策略权重滑块的百分比互转 — 与因子评分编辑器 (ScoringEditor) 同一套交互口径:
 * 滑块按 0-100 百分比自由拖动, 总和允许 ≠100 (UI 颜色提示), 保存时自动按比例归一。
 *
 * 与因子版的差异: 子策略权重为 0 仍是有效成员 (保留在列表, 不参与融合),
 * 因此归一时不过滤 0 项; 全部为 0 时退化为均等 (与后端 _effective_weights
 * total_w<=0 的均等语义一致)。
 */

/** 任意正权重数组 → 合计恰为 100 的整数百分比 (最大余数法分配残差)。 */
export function toPercentages(weights: number[]): number[] {
  const values = weights.map(w => Math.max(0, Number(w) || 0))
  const total = values.reduce((sum, v) => sum + v, 0)
  if (total <= 0) return values.map(() => 0)
  const exact = values.map(v => (v / total) * 100)
  const floors = exact.map(v => Math.floor(v))
  let remaining = 100 - floors.reduce((sum, v) => sum + v, 0)
  const order = exact
    .map((v, i) => ({ i, rem: v - Math.floor(v) }))
    .sort((a, b) => b.rem - a.rem || a.i - b.i)
  for (const { i } of order) {
    if (remaining <= 0) break
    floors[i] += 1
    remaining -= 1
  }
  return floors
}

/** 滑块百分比 → 归一小数权重 (合计=1); 保留全部成员, 全 0 时均等。 */
export function normalizeWeights(pcts: number[]): number[] {
  const values = pcts.map(w => Math.max(0, Number(w) || 0))
  const total = values.reduce((sum, v) => sum + v, 0)
  if (total <= 0) return values.map(() => +(1 / values.length).toFixed(6))
  return values.map(v => +(v / total).toFixed(6))
}
