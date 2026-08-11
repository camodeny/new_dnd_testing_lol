import ReactMarkdown from 'react-markdown'
import { memo } from 'react'
import remarkBreaks from 'remark-breaks'
import remarkGfm from 'remark-gfm'

const components = {
  a({ href, children, title }) {
    return (
      <a href={href} title={title} target="_blank" rel="noreferrer">
        {children}
      </a>
    )
  },
}

function MarkdownContent({ content }) {
  return (
    <div className="markdown-content">
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]} components={components}>
        {String(content ?? '')}
      </ReactMarkdown>
    </div>
  )
}

export default memo(MarkdownContent)
