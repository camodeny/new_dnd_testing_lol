import { useRef, useEffect, useCallback } from 'react'
import { parseQuotedMessage } from '../../utils/messageTags'

function getPlainText(element) {
  let text = ''
  for (const node of element.childNodes) {
    if (node.nodeType === Node.TEXT_NODE) {
      text += node.textContent
    } else if (node.nodeType === Node.ELEMENT_NODE) {
      if (node.tagName === 'BR') {
        text += '\n'
      } else {
        text += getPlainText(node)
      }
    }
  }
  return text
}

function renderHighlighted(element, text) {
  const segments = parseQuotedMessage(text, { includeQuoteMarks: true })
  element.innerHTML = ''

  let hasContent = false
  segments.forEach((segment) => {
    if (!segment.text) return
    hasContent = true

    const lines = segment.text.split('\n')
    lines.forEach((line, lineIndex) => {
      if (line) {
        const span = document.createElement('span')
        span.textContent = line
        if (segment.type === 'ic') {
          span.className = 'session-input-ic-highlight'
        }
        element.appendChild(span)
      }
      if (lineIndex < lines.length - 1) {
        element.appendChild(document.createElement('br'))
      }
    })
  })

  if (!hasContent) {
    element.innerHTML = ''
  }
}

function saveCursorOffset(element) {
  const selection = window.getSelection()
  if (!selection.rangeCount) return null

  const range = selection.getRangeAt(0)
  if (!element.contains(range.startContainer)) return null

  if (range.startContainer === element) {
    let offset = 0
    for (let i = 0; i < range.startOffset; i++) {
      const child = element.childNodes[i]
      if (child.nodeType === Node.TEXT_NODE) {
        offset += child.length
      } else if (child.tagName === 'BR') {
        offset += 1
      } else if (child.nodeType === Node.ELEMENT_NODE) {
        const walker = document.createTreeWalker(child, NodeFilter.SHOW_TEXT)
        let n
        while ((n = walker.nextNode())) {
          offset += n.length
        }
      }
    }
    return offset
  }

  let offset = 0
  const walker = document.createTreeWalker(
    element,
    NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT
  )
  let node
  while ((node = walker.nextNode())) {
    if (node === range.startContainer) {
      return offset + range.startOffset
    }
    if (node.nodeType === Node.TEXT_NODE) {
      offset += node.length
    } else if (node.tagName === 'BR') {
      offset += 1
    }
  }

  return offset
}

function restoreCursorOffset(element, targetOffset) {
  if (targetOffset === null || targetOffset === undefined) return

  let offset = 0
  const walker = document.createTreeWalker(
    element,
    NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT
  )
  let node
  while ((node = walker.nextNode())) {
    const len =
      node.nodeType === Node.TEXT_NODE
        ? node.length
        : node.tagName === 'BR'
        ? 1
        : 0

    if (offset + len >= targetOffset) {
      const range = document.createRange()
      if (node.nodeType === Node.TEXT_NODE) {
        range.setStart(node, Math.max(0, Math.min(targetOffset - offset, node.length)))
      } else if (node.tagName === 'BR') {
        if (targetOffset > offset) {
          range.setStartAfter(node)
        } else {
          range.setStartBefore(node)
        }
      }
      range.collapse(true)

      const sel = window.getSelection()
      sel.removeAllRanges()
      sel.addRange(range)
      return
    }
    offset += len
  }

  const range = document.createRange()
  range.selectNodeContents(element)
  range.collapse(false)
  const sel = window.getSelection()
  sel.removeAllRanges()
  sel.addRange(range)
}

export default function SessionInput({ value, onChange, onSubmit, disabled, placeholder }) {
  const ref = useRef(null)
  const isComposing = useRef(false)

  // Sync external value changes to DOM (e.g. send clears input)
  useEffect(() => {
    if (!ref.current || isComposing.current) return
    const currentText = getPlainText(ref.current)
    if (currentText !== value) {
      renderHighlighted(ref.current, value)
      if (!value) {
        const range = document.createRange()
        range.setStart(ref.current, 0)
        range.collapse(true)
        const sel = window.getSelection()
        sel.removeAllRanges()
        sel.addRange(range)
      }
    }
  }, [value])

  const handleInput = useCallback(() => {
    if (!ref.current || isComposing.current) return

    const text = getPlainText(ref.current)
    const offset = saveCursorOffset(ref.current)

    renderHighlighted(ref.current, text)
    restoreCursorOffset(ref.current, offset)

    onChange(text)
  }, [onChange])

  const handleKeyDown = useCallback(
    (e) => {
      // Block rich-text formatting shortcuts
      if ((e.ctrlKey || e.metaKey) && ['b', 'i', 'u'].includes(e.key.toLowerCase())) {
        e.preventDefault()
        return
      }

      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault()
        onSubmit()
        return
      }

      if (e.key === 'Enter' && e.shiftKey) {
        e.preventDefault()
        document.execCommand('insertHTML', false, '<br>')
        return
      }
    },
    [onSubmit]
  )

  const handlePaste = useCallback((e) => {
    e.preventDefault()
    const text = e.clipboardData.getData('text/plain')
    document.execCommand('insertText', false, text)
  }, [])

  const showPlaceholder = !value && !disabled

  return (
    <>
      <div
        ref={ref}
        className="session-input-editable"
        contentEditable={!disabled}
        suppressContentEditableWarning
        onInput={handleInput}
        onKeyDown={handleKeyDown}
        onPaste={handlePaste}
        onCompositionStart={() => {
          isComposing.current = true
        }}
        onCompositionEnd={() => {
          isComposing.current = false
          handleInput()
        }}
        role="textbox"
        aria-multiline="true"
        aria-disabled={disabled}
        tabIndex={disabled ? -1 : 0}
      />
      {showPlaceholder && (
        <div className="session-input-placeholder" aria-hidden="true">
          {placeholder}
        </div>
      )}
    </>
  )
}
