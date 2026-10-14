/**
 * NPC dialogue markup in DM narration.
 *
 * The narrator wraps each NPC line as `<npc speaker="Name">"…"</npc>`
 * (backend `app/dm/npc_markup.py` owns the format). This splits narration
 * into prose and dialogue parts so dialogue renders as its own block.
 *
 * Forgiving by design, since narration streams in: an opening tag with no
 * closing tag yet is a line still arriving, a half-written tag at the very
 * end is hidden until it completes, and stray tags never show as text.
 */

export type DmNarrationPart =
  | { type: 'prose'; text: string }
  | { type: 'npc'; speaker: string; text: string }

const OPEN_TAG = /<npc\b([^>]*)>/i
const CLOSE_TAG = /<\/npc\s*>/i
const ANY_TAG = /<\/?npc\b[^>]*>/gi
const SPEAKER_ATTR = /\bspeaker\s*=\s*(?:"([^"]*)"|'([^']*)')/i
/** `<`, `<np`, `<npc speaker="Ma`, `</np` — a tag cut off mid-stream. */
const TRAILING_TAG_FRAGMENT = /<\/?(?:n(?:p(?:c[^>]*)?)?)?$/i

const ENTITIES: Record<string, string> = { '&quot;': '"', '&#x27;': "'", '&#39;': "'", '&lt;': '<', '&gt;': '>', '&amp;': '&' }

function speakerOf(attributes: string): string {
  const match = SPEAKER_ATTR.exec(attributes)
  const raw = match ? (match[1] ?? match[2] ?? '') : ''
  return raw.replace(/&(?:quot|#x27|#39|lt|gt|amp);/g, (entity) => ENTITIES[entity]).trim()
}

export function parseDmNarration(content: string): DmNarrationPart[] {
  const parts: DmNarrationPart[] = []
  const pushProse = (text: string) => {
    const cleaned = text.replace(ANY_TAG, '')
    if (cleaned.trim()) parts.push({ type: 'prose', text: cleaned })
  }
  let rest = content
  while (rest) {
    const open = OPEN_TAG.exec(rest)
    if (!open) {
      pushProse(rest.replace(TRAILING_TAG_FRAGMENT, ''))
      break
    }
    pushProse(rest.slice(0, open.index))
    const speaker = speakerOf(open[1])
    const body = rest.slice(open.index + open[0].length)
    const close = CLOSE_TAG.exec(body)
    const text = (close ? body.slice(0, close.index) : body.replace(TRAILING_TAG_FRAGMENT, ''))
      .replace(ANY_TAG, '')
      .trim()
    if (text || (!close && speaker)) parts.push({ type: 'npc', speaker, text })
    if (!close) break
    rest = body.slice(close.index + close[0].length)
  }
  return parts
}
