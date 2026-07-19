import Papa from 'papaparse'

export type CsvTable = {
  headers: string[]
  rows: string[][]
  /** True when the first CSV row was treated as column headers. */
  hasHeader: boolean
}

function normalizeRow(row: unknown[], width: number): string[] {
  const out: string[] = []
  for (let i = 0; i < width; i += 1) {
    const cell = row[i]
    out.push(cell == null ? '' : String(cell))
  }
  return out
}

function looksLikeHeader(cells: string[]): boolean {
  if (!cells.length) return false
  // Header-ish if most cells are short-ish non-numeric labels.
  let score = 0
  for (const cell of cells) {
    const trimmed = cell.trim()
    if (!trimmed) continue
    if (/^-?\d+(\.\d+)?$/.test(trimmed)) continue
    if (trimmed.length <= 40) score += 1
  }
  return score >= Math.ceil(cells.filter(c => c.trim()).length * 0.6)
}

/**
 * Parse CSV text into a rectangular table. Uses the first row as headers when
 * it looks like one; otherwise synthesizes Column 1..N labels.
 */
export function parseCsv(text: string): CsvTable {
  const parsed = Papa.parse<string[]>(text ?? '', {
    header: false,
    skipEmptyLines: 'greedy',
    dynamicTyping: false,
  })

  const rawRows = (parsed.data || [])
    .filter((row): row is string[] => Array.isArray(row))
    .map(row => row.map(cell => (cell == null ? '' : String(cell))))

  if (!rawRows.length) {
    return { headers: [], rows: [], hasHeader: false }
  }

  const width = Math.max(...rawRows.map(r => r.length), 0)
  const normalized = rawRows.map(r => normalizeRow(r, width))
  const first = normalized[0]

  if (looksLikeHeader(first)) {
    const headers = first.map((h, i) => h.trim() || `Column ${i + 1}`)
    return {
      headers,
      rows: normalized.slice(1),
      hasHeader: true,
    }
  }

  return {
    headers: Array.from({ length: width }, (_, i) => `Column ${i + 1}`),
    rows: normalized,
    hasHeader: false,
  }
}

/** Serialize a table back to CSV text (header row included when hasHeader). */
export function serializeCsv(table: CsvTable): string {
  const data = table.hasHeader
    ? [table.headers, ...table.rows]
    : table.rows

  if (!data.length) return ''

  return Papa.unparse(data, {
    quotes: false,
    quoteChar: '"',
    escapeChar: '"',
    delimiter: ',',
    newline: '\n',
  })
}

export function isCsvPath(filePath: string): boolean {
  return /\.csv$/i.test(filePath.replace(/:\d+$/, ''))
}

/** 0-indexed column → Excel-style letter (0→A, 25→Z, 26→AA). */
export function excelColLetter(colIndex: number): string {
  let n = Math.max(0, Math.floor(colIndex))
  let out = ''
  do {
    out = String.fromCharCode(65 + (n % 26)) + out
    n = Math.floor(n / 26) - 1
  } while (n >= 0)
  return out
}

/** Agent-facing cell locator, e.g. `row 12, column superpower [G]`. */
export function formatCsvCellAnchor(opts: {
  row: number
  colIndex: number
  colHeader?: string | null
}): string {
  const header = (opts.colHeader || '').trim() || `Column ${opts.colIndex + 1}`
  const letter = excelColLetter(opts.colIndex)
  return `row ${opts.row}, column ${header} [${letter}]`
}
