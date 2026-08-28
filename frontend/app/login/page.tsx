'use client'

import { useAuthContext } from '@/contexts/AuthContext'
import LoginPage from './LoginPage'

export default function LoginRoute() {
  const { setUser } = useAuthContext()
  return <LoginPage onLogin={setUser} />
}
