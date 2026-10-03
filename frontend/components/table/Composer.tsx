'use client'

import { useEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { tokenizeForHighlight } from '@/lib/icOoc'

export type ComposerMode = 'do' | 'say'

interface ComposerProps {
  value: string
  onChange: (value: string) => void
  mode: ComposerMode
  onModeChange: (mode: ComposerMode) => void
  onSubmit: () => void
  inputRef: RefObject<HTMLTextAreaElement | null>
  disabled?: boolean
  sendDisabled?: boolean
  /** Issue #255: AI narration paused; the draft stays editable. */
  aiPaused?: boolean
  placeholder?: string
}

const QUOTE = /["“”]/

/** "Say" sends the draft as speech unless the player already quoted it. */
export function composeForMode(text: string, mode: ComposerMode): string {
  return mode === 'say' && !QUOTE.test(text) ? `"${text}"` : text
}

/**
 * Story composer. Quoted text renders in an in-character box via a
 * metrically identical backdrop behind a transparent textarea; "Say" mode
 * treats the whole draft as speech so newcomers never need the quote trick.
 */
export default function Composer({
  value, onChange, mode, onModeChange, onSubmit, inputRef,
  disabled = false, sendDisabled = false, aiPaused = false, placeholder,
}: ComposerProps) {
  const highlightRef = useRef<HTMLDivElement>(null)
  // Collapsed caret position (null when blurred or a range is selected).
  // The backdrop draws its own caret so the native one never renders over
  // backdrop glyphs.
  const [caret, setCaret] = useState<number | null>(null)

  // Auto-grow with newlines up to ~10 rows, then scroll; the backdrop
  // mirrors the textarea scroll position.
  useEffect(() => {
    const ta = inputRef.current
    if (!ta) return
    ta.style.height = 'auto'
    const max = Math.round(10 * 0.9 * 16 * 1.5)
    ta.style.height = `${Math.min(ta.scrollHeight, max)}px`
    ta.style.overflowY = ta.scrollHeight > max ? 'auto' : 'hidden'
    syncScroll()
  }, [value, inputRef])

  const syncScroll = () => {
    const ta = inputRef.current
    if (ta && highlightRef.current) {
      highlightRef.current.scrollTop = ta.scrollTop
      highlightRef.current.scrollLeft = ta.scrollLeft
    }
  }

  const readCaret = (ta: HTMLTextAreaElement | null): number | null => {
    if (!ta || ta.selectionStart !== ta.selectionEnd) return null
    return ta.selectionStart
  }

  const renderHighlight = (): ReactNode => {
    const tokens = mode === 'say' && value ? [{ text: value, ic: true }] : tokenizeForHighlight(value)
    const nodes: ReactNode[] = []
    let offset = 0
    let caretPlaced = caret === null
    tokens.forEach((token, index) => {
      const start = offset
      offset += token.text.length
      let inner: ReactNode = token.text
      if (!caretPlaced && caret !== null && caret >= start && caret <= start + token.text.length) {
        // Split at the caret and park a net-zero-width marker there so no
        // glyph drifts.
        const local = caret - start
        inner = <>{token.text.slice(0, local)}<span className="composer-fake-caret" />{token.text.slice(local)}</>
        caretPlaced = true
      }
      nodes.push(token.ic
        ? <span key={index} className="composer-ic">{inner}</span>
        : <span key={index}>{inner}</span>)
    })
    if (!caretPlaced) nodes.push(<span key="caret" className="composer-fake-caret" />)
    return nodes
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      onSubmit()
    }
  }

  const defaultPlaceholder = mode === 'say' ? 'What do you say?' : 'What do you do?'

  return (
    <div className="tv-composer">
      {aiPaused && (
        <p role="status" className="tv-paused-note">
          AI narration is paused — your words stay here as a draft until capacity returns.
        </p>
      )}
      <div className="composer-wrap">
        <div ref={highlightRef} className="composer-backdrop" aria-hidden="true">
          {renderHighlight()}
          {/* Zero-width space preserves a trailing newline's height. */}
          {'​'}
        </div>
        <textarea
          ref={inputRef}
          className="session-input-editable composer-transparent tv-composer-input"
          placeholder={aiPaused ? 'Draft while paused…' : (placeholder ?? defaultPlaceholder)}
          value={value}
          onChange={(e) => { onChange(e.target.value); setCaret(readCaret(e.target)) }}
          onSelect={(e) => setCaret(readCaret(e.target as HTMLTextAreaElement))}
          onFocus={(e) => setCaret(readCaret(e.target))}
          onBlur={() => setCaret(null)}
          onScroll={syncScroll}
          onKeyDown={handleKeyDown}
          rows={1}
          disabled={disabled}
          aria-label={aiPaused ? 'Message draft (AI narration paused)' : 'Message'}
          spellCheck={false}
        />
      </div>
      <div className="tv-composer-row">
        <div className="tv-mode" role="radiogroup" aria-label="Message type">
          {(['do', 'say'] as const).map((m) => (
            <button
              key={m}
              type="button"
              role="radio"
              aria-checked={mode === m}
              className={mode === m ? 'on' : undefined}
              onClick={() => { onModeChange(m); inputRef.current?.focus() }}
            >
              {m === 'do' ? 'Do' : 'Say'}
            </button>
          ))}
        </div>
        <button
          type="button"
          className="tv-send"
          onClick={onSubmit}
          disabled={sendDisabled}
          aria-label={aiPaused ? 'Send unavailable while AI narration is paused' : 'Send message'}
          title={aiPaused ? 'AI narration is paused — your draft is kept' : undefined}
        >
          <i className="bi bi-arrow-up" aria-hidden="true" />
        </button>
      </div>
    </div>
  )
}
