export function parseDate(iso) {
  if (!iso) return null
  let dateStr = iso
  if (typeof dateStr === 'string') {
    if (dateStr.includes('T') && !dateStr.endsWith('Z') && !/[+-]\d{2}:?\d{2}$/.test(dateStr)) {
      dateStr = dateStr + 'Z'
    }
  }
  return new Date(dateStr)
}
