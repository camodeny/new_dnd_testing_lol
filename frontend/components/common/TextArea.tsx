import { TextareaHTMLAttributes, forwardRef } from 'react'

const TextArea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(
  ({ className = '', ...props }, ref) => (
    <textarea ref={ref} className={`textarea ${className}`.trim()} {...props} />
  ),
)

TextArea.displayName = 'TextArea'
export default TextArea
