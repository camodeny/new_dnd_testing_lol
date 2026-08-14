'use client'

import * as Dialog from '@radix-ui/react-dialog'

interface ModalProps {
  open: boolean
  onClose: () => void
  title: string
  titleId?: string
  children: React.ReactNode
  maxWidth?: number
  alertDialog?: boolean
}

export default function Modal({
  open,
  onClose,
  title,
  titleId,
  children,
  maxWidth = 560,
}: ModalProps) {
  const labelId = titleId ?? 'modal-title'

  return (
    <Dialog.Root open={open} onOpenChange={(o) => { if (!o) onClose() }}>
      <Dialog.Portal>
        <Dialog.Overlay className="modal-overlay" />
        <Dialog.Content
          className="modal-panel"
          aria-labelledby={labelId}
          style={{ maxWidth }}
          onEscapeKeyDown={onClose}
          onPointerDownOutside={onClose}
        >
          <div className="modal-header">
            <Dialog.Title asChild>
              <h2 id={labelId}>{title}</h2>
            </Dialog.Title>
            <Dialog.Close asChild>
              <button className="modal-close" aria-label="Close dialog">
                <i className="bi bi-x-lg" aria-hidden="true" />
              </button>
            </Dialog.Close>
          </div>
          <div className="modal-body">{children}</div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
