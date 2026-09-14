import { useState, useEffect } from 'react'
import { useAuth } from './hooks/useAuth'
import { useConfig } from './hooks/useConfig'
import ChatPanel from './components/ChatPanel'
import DocumentPanel from './components/DocumentPanel'
import LoginPage from './components/LoginPage'

type Tab = 'chat' | 'documents' | 'settings'

export default function App() {
  const auth = useAuth()
  const config = useConfig()
  const [activeTab, setActiveTab] = useState<Tab>('chat')

  useEffect(() => {
    if (config.loaded && config.app_name) {
      document.title = config.app_name
    }
  }, [config.loaded, config.app_name])

  if (auth.loading) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center',
        minHeight: '100vh', background: 'var(--bg-deep)', color: 'var(--text-muted)' }}>
        <p>Loading...</p>
      </div>
    )
  }

  if (!auth.authenticated) {
    return <LoginPage auth={auth} />
  }

  const brandInitial = config.app_name.charAt(0).toUpperCase()

  return (
    <div style={{ display: 'flex', width: '100%', minHeight: '100vh' }}>
      {/* Sidebar */}
      <aside style={{ width: 260, borderRight: '1px solid var(--glass-border)',
        background: 'var(--bg-base)', display: 'flex', flexDirection: 'column', flexShrink: 0 }}>
        <div style={{ padding: '1rem', borderBottom: '1px solid var(--glass-border)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.6rem' }}>
            <div style={{ width: 32, height: 32, borderRadius: '50%', background: 'var(--accent-primary)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              fontSize: '0.9rem', fontWeight: 700, color: 'var(--bg-deep)' }}>
              {brandInitial}
            </div>
            <div>
              <div style={{ fontWeight: 600, fontSize: '0.9rem' }}>{config.app_name}</div>
              {config.company_name && (
                <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>{config.company_name}</div>
              )}
            </div>
          </div>
        </div>
        <nav style={{ padding: '0.5rem', flex: 1 }}>
          {(['chat', 'documents', 'settings'] as Tab[]).map(tab => (
            <button key={tab} onClick={() => setActiveTab(tab)}
              style={{
                display: 'block', width: '100%', textAlign: 'left', marginBottom: '0.25rem',
                background: activeTab === tab ? 'var(--accent-primary)' : 'transparent',
                border: 'none', padding: '0.6rem 0.8rem', borderRadius: 6,
              }}>
              {tab.charAt(0).toUpperCase() + tab.slice(1)}
            </button>
          ))}
        </nav>
        <div style={{ padding: '0.75rem', borderTop: '1px solid var(--glass-border)',
          fontSize: '0.75rem', color: 'var(--text-muted)' }}>
          {auth.user?.display_name}
          <button onClick={auth.logout} style={{ float: 'right', padding: '0.2rem 0.5rem',
            fontSize: '0.7rem', background: 'transparent', border: '1px solid var(--glass-border)' }}>
            Logout
          </button>
        </div>
      </aside>

      {/* Main */}
      <main style={{ flex: 1, overflow: 'auto' }}>
        {activeTab === 'chat' && <ChatPanel config={config} />}
        {activeTab === 'documents' && <DocumentPanel config={config} />}
        {activeTab === 'settings' && (
          <div style={{ padding: '2rem' }}>
            <h2>Settings</h2>
            <p style={{ color: 'var(--text-muted)', marginTop: '1rem' }}>
              Provider: check /api/health for current LLM configuration.
            </p>
          </div>
        )}
      </main>
    </div>
  )
}
