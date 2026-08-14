import { HTMLAttributes, forwardRef } from 'react'

interface CardProps extends HTMLAttributes<HTMLDivElement> {
  wrapper?: boolean
}

const Card = forwardRef<HTMLDivElement, CardProps>(
  ({ wrapper, className = '', children, ...props }, ref) => (
    <div
      ref={ref}
      className={`${wrapper ? 'card-wrapper' : 'card'} ${className}`.trim()}
      {...props}
    >
      {children}
    </div>
  ),
)

Card.displayName = 'Card'
export default Card
