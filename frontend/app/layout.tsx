import type { Metadata } from 'next'
import { DM_Sans, Fraunces } from 'next/font/google'
import 'bootstrap-icons/font/bootstrap-icons.css'
import './globals.css'
import AuthProvider from '@/components/providers/AuthProvider'
import AppShell from '@/components/layout/AppShell'

const dmSans = DM_Sans({
  subsets: ['latin'],
  weight: 'variable',
  axes: ['opsz'],
  variable: '--font-dm-sans',
  display: 'swap',
})

const fraunces = Fraunces({
  subsets: ['latin'],
  weight: 'variable',
  axes: ['opsz'],
  variable: '--font-fraunces',
  display: 'swap',
})

export const metadata: Metadata = {
  title: 'Fireside',
  description: 'Gather your party. The table is open.',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${dmSans.variable} ${fraunces.variable}`}>
      <body>
        <AuthProvider>
          <AppShell>{children}</AppShell>
        </AuthProvider>
      </body>
    </html>
  )
}
