import { InputHTMLAttributes, forwardRef } from 'react'

const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  ({ className = '', ...props }, ref) => (
    <input ref={ref} className={`input ${className}`.trim()} {...props} />
  ),
)

Input.displayName = 'Input'
export default Input
