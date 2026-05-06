const TAG_PATTERN = /<(ic|ooc)>([\s\S]*?)<\/\1>/gi

function normalizeText(text) {
  return typeof text === 'string' ? text : ''
}

function pushSegment(segments, type, text, { trim = false } = {}) {
  const value = trim ? text.trim() : text
  if (!value) return

  const last = segments[segments.length - 1]
  if (last?.type === type) {
    last.text += value
    return
  }

  segments.push({ type, text: value })
}

function isOpeningQuote(char) {
  return char === '"' || char === '\u201c' || char === '\u201d'
}

function isClosingQuote(char, opener) {
  if (opener === '"') return char === '"'
  return char === '\u201d'
}

export function parseQuotedMessage(text, options = {}) {
  const value = normalizeText(text)
  const segments = []
  let oocBuffer = ''
  let icBuffer = ''
  let opener = ''

  for (const char of value) {
    if (!opener && isOpeningQuote(char)) {
      pushSegment(segments, 'ooc', oocBuffer)
      oocBuffer = ''
      opener = char
      icBuffer = options.includeQuoteMarks ? char : ''
      continue
    }

    if (opener && isClosingQuote(char, opener)) {
      pushSegment(segments, 'ic', options.includeQuoteMarks ? `${icBuffer}${char}` : icBuffer)
      icBuffer = ''
      opener = ''
      continue
    }

    if (opener) {
      icBuffer += char
    } else {
      oocBuffer += char
    }
  }

  if (opener) {
    oocBuffer += icBuffer
  }

  pushSegment(segments, 'ooc', oocBuffer)
  return segments
}

export function parseTaggedMessage(text) {
  const value = normalizeText(text)
  if (!/<\/?(?:ic|ooc)>/i.test(value)) {
    return parseQuotedMessage(value)
  }

  const segments = []
  let index = 0
  TAG_PATTERN.lastIndex = 0

  for (const match of value.matchAll(TAG_PATTERN)) {
    if (match.index > index) {
      pushSegment(segments, 'ooc', value.slice(index, match.index))
    }
    pushSegment(segments, match[1].toLowerCase(), match[2])
    index = match.index + match[0].length
  }

  if (index < value.length) {
    pushSegment(segments, 'ooc', value.slice(index))
  }

  return segments
}

export function formatMessageForDm(text) {
  return parseQuotedMessage(text)
    .map((segment) => {
      const content = segment.text.trim()
      return content ? `<${segment.type}>${content}</${segment.type}>` : ''
    })
    .filter(Boolean)
    .join('')
}

export function hasIcSegment(text) {
  return parseQuotedMessage(text).some((segment) => segment.type === 'ic' && segment.text.trim())
}
