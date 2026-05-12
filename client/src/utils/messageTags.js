const TAG_PATTERN = /<(ic|ooc|npc)\b([^>]*)>([\s\S]*?)<\/\1>/gi
const TAG_MARKER_PATTERN = /<\/?(?:ic|ooc|npc)\b/i

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

function parseTagAttributes(rawAttrs = '') {
  const attrs = {}
  const attrPattern = /([a-zA-Z_][\w:-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>/]+))/g

  for (const match of rawAttrs.matchAll(attrPattern)) {
    attrs[match[1].toLowerCase()] = match[2] ?? match[3] ?? match[4] ?? ''
  }

  return attrs
}

function pushTaggedSegment(segments, type, attrs, text) {
  if (type === 'npc') {
    const name = attrs.target || attrs.name || attrs.actor || 'NPC'
    const value = text.trim()
    if (!value) return
    segments.push({ type, text: value, target: name.trim() || 'NPC', attrs })
    return
  }

  pushSegment(segments, type, text)
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
  if (!TAG_MARKER_PATTERN.test(value)) {
    return parseQuotedMessage(value)
  }

  const segments = []
  let index = 0
  TAG_PATTERN.lastIndex = 0

  for (const match of value.matchAll(TAG_PATTERN)) {
    if (match.index > index) {
      pushSegment(segments, 'ooc', value.slice(index, match.index))
    }
    pushTaggedSegment(segments, match[1].toLowerCase(), parseTagAttributes(match[2]), match[3])
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
