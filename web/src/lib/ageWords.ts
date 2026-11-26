/** An age in days as words: "12 days", "3 months", "2 years". */
export function ageInWords(days: number): string {
  const d = Math.max(0, Math.floor(days))
  if (d < 30) return `${d} ${d === 1 ? 'day' : 'days'}`
  if (d < 365) {
    const months = Math.floor(d / 30)
    return `${months} ${months === 1 ? 'month' : 'months'}`
  }
  const years = Math.floor(d / 365)
  return `${years} ${years === 1 ? 'year' : 'years'}`
}
