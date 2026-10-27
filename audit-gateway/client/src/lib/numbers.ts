export function exactNumber (value = 0): string {
  return value.toLocaleString('zh-CN')
}

export function compactNumber (value = 0): string {
  const normalized = value
  const units = [
    { threshold: 1_000_000_000, suffix: 'B' },
    { threshold: 1_000_000, suffix: 'M' },
    { threshold: 1000, suffix: 'K' },
  ]
  const unit = units.find(item => Math.abs(normalized) >= item.threshold)
  if (!unit) {
    return exactNumber(normalized)
  }

  const scaled = normalized / unit.threshold
  const digits = Math.abs(scaled) >= 100 ? 0 : (Math.abs(scaled) >= 10 ? 1 : 2)
  return `${scaled.toFixed(digits).replace(/\.0+$|(?<=\.\d)0+$/, '')}${unit.suffix}`
}
