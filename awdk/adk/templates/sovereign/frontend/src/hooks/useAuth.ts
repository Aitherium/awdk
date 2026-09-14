import { useState, useEffect } from 'react'

interface User {
  user_id: string
  email: string
  display_name: string
  tenant_id: string
  role: string
}

interface AuthState {
  loading: boolean
  authenticated: boolean
  user: User | null
  login: () => Promise<void>
  logout: () => Promise<void>
}

export function useAuth(): AuthState {
  const [loading, setLoading] = useState(true)
  const [user, setUser] = useState<User | null>(null)

  useEffect(() => {
    fetch('/api/auth/me')
      .then(r => r.json())
      .then(data => {
        if (data.authenticated) {
          setUser(data)
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  const login = async () => {
    const r = await fetch('/api/auth/dev-login', { method: 'POST' })
    const data = await r.json()
    if (data.authenticated) {
      setUser({
        user_id: 'dev-user',
        email: 'dev@local',
        display_name: data.display_name,
        tenant_id: 'default',
        role: data.role,
      })
    }
  }

  const logout = async () => {
    await fetch('/api/auth/logout', { method: 'POST' })
    setUser(null)
  }

  return { loading, authenticated: !!user, user, login, logout }
}
