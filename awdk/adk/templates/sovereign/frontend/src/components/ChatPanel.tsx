import { useState, useRef, useEffect } from 'react'

interface Message {
  role: 'user' | 'assistant'
  content: string
  sources?: { filename: string; score: number }[]
}

export default function ChatPanel({ config }: { config: any }) {
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [conversationId, setConversationId] = useState<string | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages])

  const send = async () => {
    if (!input.trim() || loading) return
    const msg = input.trim()
    setInput('')
    setMessages(prev => [...prev, { role: 'user', content: msg }])
    setLoading(true)

    try {
      const r = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: msg, conversation_id: conversationId }),
      })
      const data = await r.json()
      if (!conversationId) setConversationId(data.conversation_id)
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: data.response,
        sources: data.sources,
      }])
    } catch {
      setMessages(prev => [...prev, { role: 'assistant', content: 'Error: could not reach the server.' }])
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh' }}>
      <div style={{ flex: 1, overflow: 'auto', padding: '1.5rem' }}>
        {messages.length === 0 && config.welcome_message && (
          <div style={{ color: 'var(--text-muted)', padding: '2rem', textAlign: 'center' }}>
            <p style={{ whiteSpace: 'pre-line' }}>{config.welcome_message}</p>
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} style={{
            marginBottom: '1rem',
            textAlign: m.role === 'user' ? 'right' : 'left',
          }}>
            <div style={{
              display: 'inline-block', maxWidth: '70%', padding: '0.75rem 1rem',
              borderRadius: 12,
              background: m.role === 'user' ? 'var(--accent-primary)' : 'var(--bg-surface)',
              whiteSpace: 'pre-wrap', fontSize: '0.9rem', lineHeight: 1.5,
            }}>
              {m.content}
            </div>
            {m.sources && m.sources.length > 0 && (
              <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)', marginTop: '0.3rem' }}>
                Sources: {m.sources.map(s => s.filename).join(', ')}
              </div>
            )}
          </div>
        ))}
        {loading && (
          <div style={{ color: 'var(--text-muted)', fontSize: '0.85rem' }}>Thinking...</div>
        )}
        <div ref={bottomRef} />
      </div>
      <div style={{ padding: '1rem', borderTop: '1px solid var(--glass-border)',
        display: 'flex', gap: '0.5rem' }}>
        <input
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && !e.shiftKey && send()}
          placeholder="Ask a question..."
          style={{ flex: 1 }}
        />
        <button onClick={send} disabled={loading}>Send</button>
      </div>
    </div>
  )
}
