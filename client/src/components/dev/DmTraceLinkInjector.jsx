import { useEffect } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

const TRACE_BUTTON_ATTR = 'data-dm-trace-button'
const TRACE_ATTACHED_ATTR = 'data-dm-trace-attached'

function dmTextFromMessageNode(node) {
  const contentNode = node.querySelector('.session-dm-tagged-content') || node.querySelector('.session-msg-content')
  return (contentNode?.innerText || '').trim()
}

function applyTraceButtonStyles(button) {
  button.style.marginTop = '0.5rem'
  button.style.display = 'inline-flex'
  button.style.alignItems = 'center'
  button.style.gap = '0.35rem'
  button.style.border = '1px solid rgba(148, 163, 184, 0.35)'
  button.style.borderRadius = '999px'
  button.style.background = 'rgba(15, 23, 42, 0.6)'
  button.style.color = '#93c5fd'
  button.style.padding = '0.25rem 0.55rem'
  button.style.fontSize = '0.75rem'
  button.style.cursor = 'pointer'
}

export default function DmTraceLinkInjector() {
  const location = useLocation()
  const navigate = useNavigate()

  useEffect(() => {
    const match = location.pathname.match(/^\/campaigns\/(\d+)$/)
    if (!match) return undefined

    const campaignId = match[1]

    const attachButtons = () => {
      document.querySelectorAll('.session-msg.session-msg-dm').forEach((node) => {
        if (node.getAttribute(TRACE_ATTACHED_ATTR) === 'true') return
        const body = node.querySelector('.session-msg-body')
        if (!body) return

        node.setAttribute(TRACE_ATTACHED_ATTR, 'true')
        const button = document.createElement('button')
        const icon = document.createElement('i')
        const label = document.createElement('span')
        button.type = 'button'
        button.setAttribute(TRACE_BUTTON_ATTR, 'true')
        button.setAttribute('aria-label', 'View DM turn trace')
        button.title = 'View DM turn trace'
        icon.className = 'bi bi-clock-history'
        label.textContent = 'Trace'
        button.appendChild(icon)
        button.appendChild(label)
        applyTraceButtonStyles(button)
        button.addEventListener('click', () => {
          const text = dmTextFromMessageNode(node)
          const params = new URLSearchParams()
          if (text) params.set('dmText', text.slice(0, 500))
          navigate(`/campaigns/${campaignId}/dev/dm-turns?${params.toString()}`)
        })
        body.appendChild(button)
      })
    }

    attachButtons()
    const observer = new MutationObserver(attachButtons)
    observer.observe(document.body, { childList: true, subtree: true })

    return () => {
      observer.disconnect()
      document.querySelectorAll(`[${TRACE_BUTTON_ATTR}="true"]`).forEach((button) => button.remove())
      document.querySelectorAll(`[${TRACE_ATTACHED_ATTR}="true"]`).forEach((node) => node.removeAttribute(TRACE_ATTACHED_ATTR))
    }
  }, [location.pathname, navigate])

  return null
}
