/**
 * Quote-to-IC parsing for the live-table composer.
 *
 * Convention: `"double-quoted"` (straight) or `“curly-quoted”` text is
 * in-character (IC); everything else is out-of-character (OOC) table talk.
 * The frontend owns this parsing and sends typed `segments` to the backend,
 * which stays authoritative on `<ic>/<ooc>` tags and explicit segments.
 *
 * Forgiving by design: an unmatched opener, or an empty/whitespace-only
 * quoted span, is kept as literal OOC text — never a validation error.
 */

export interface IcOocSegment {
  type: 'ic' | 'ooc'
  text: string
}

export interface HighlightToken {
  /** Exact source slice, delimiters included on IC tokens. */
  text: string
  ic: boolean
}

const STRAIGHT_QUOTE = '"'
const OPEN_CURLY = '“'
const CLOSE_CURLY = '”'

function isOpener(char: string): boolean {
  return char === STRAIGHT_QUOTE || char === OPEN_CURLY
}

function isCloser(char: string): boolean {
  return char === STRAIGHT_QUOTE || char === CLOSE_CURLY
}

interface ScanPart {
  text: string
  /** True when wrapped in a matched quote pair with non-blank inner text. */
  ic: boolean
  /** The raw source slice, quote delimiters included. */
  raw: string
}

function scan(content: string): ScanPart[] {
  const parts: ScanPart[] = []
  let oocBuffer = ''
  let inQuote = false
  let quoteStart = 0
  let icBuffer = ''

  const flushOoc = () => {
    if (oocBuffer) {
      parts.push({ text: oocBuffer, ic: false, raw: oocBuffer })
      oocBuffer = ''
    }
  }

  for (let index = 0; index < content.length; index += 1) {
    const char = content[index]
    if (!inQuote && isOpener(char)) {
      inQuote = true
      quoteStart = index
      icBuffer = ''
    } else if (inQuote && isCloser(char)) {
      const raw = content.slice(quoteStart, index + 1)
      if (icBuffer.trim()) {
        flushOoc()
        parts.push({ text: icBuffer, ic: true, raw })
      } else {
        // Empty quotes ("") are literal OOC text, never an empty IC
        // segment (the backend rejects empty segment text).
        oocBuffer += raw
      }
      inQuote = false
      icBuffer = ''
    } else if (inQuote) {
      icBuffer += char
    } else {
      oocBuffer += char
    }
  }
  if (inQuote) {
    // Unmatched opener: forgiving — the whole run stays literal OOC text.
    oocBuffer += content.slice(quoteStart)
  }
  flushOoc()
  return parts
}

/**
 * Parse composer text into typed IC/OOC segments for the submissions API.
 * Quote delimiters are stripped from IC text; `raw_content` keeps them.
 * Always returns at least one non-empty segment.
 */
export function parseQuotedSegments(content: string): IcOocSegment[] {
  const segments: IcOocSegment[] = []
  for (const part of scan(content)) {
    const previous = segments[segments.length - 1]
    if (previous && (previous.type === 'ic') === part.ic) {
      previous.text += part.text
    } else {
      segments.push({ type: part.ic ? 'ic' : 'ooc', text: part.text })
    }
  }
  if (segments.length === 0) return [{ type: 'ooc', text: content }]
  return segments
}

/** Tokenize composer text for the live highlight overlay (keeps quotes). */
export function tokenizeForHighlight(content: string): HighlightToken[] {
  const tokens: HighlightToken[] = []
  for (const part of scan(content)) {
    const previous = tokens[tokens.length - 1]
    // Merge adjacent OOC runs so the backdrop stays a small node list;
    // IC spans stay discrete so each gets its own colored box.
    if (!part.ic && previous && !previous.ic) {
      previous.text += part.raw
    } else {
      tokens.push({ text: part.raw, ic: part.ic })
    }
  }
  return tokens
}

/** True when at least one quoted IC span is present. */
export function hasIcSpan(content: string): boolean {
  return scan(content).some((part) => part.ic)
}
