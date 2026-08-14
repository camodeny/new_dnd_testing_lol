import { ButtonHTMLAttributes, forwardRef } from 'react'

type Variant = 'primary' | 'secondary' | 'danger' | 'ghost-light'
type Size = 'default' | 'small'

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant
  size?: Size
  loading?: boolean
}

const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ variant = 'secondary', size = 'default', loading, className = '', children, disabled, ...props }, ref) => {
    const variantClass = {
      primary: 'btn-primary',
      secondary: 'btn-secondary',
      danger: 'btn-danger',
      'ghost-light': 'btn-ghost-light',
    }[variant]

    const sizeClass = size === 'small' ? 'small' : ''

    return (
      <button
        ref={ref}
        className={`btn ${variantClass} ${sizeClass} ${className}`.trim()}
        disabled={disabled || loading}
        {...props}
      >
        {loading ? <i className="bi bi-arrow-repeat" style={{ animation: 'spin 0.8s linear infinite' }} /> : null}
        {children}
      </button>
    )
  },
)

Button.displayName = 'Button'
export default Button
