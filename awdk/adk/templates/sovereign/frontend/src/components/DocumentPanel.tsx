import { useState, useEffect } from 'react'

interface Doc {
  id: string
  filename: string
  doc_type: string
  uploaded_at: string | null
  chunk_count: number
}

export default function DocumentPanel({ config }: { config: any }) {
  const [docs, setDocs] = useState<Doc[]>([])
  const [uploading, setUploading] = useState(false)

  const loadDocs = () => {
    fetch('/api/documents').then(r => r.json()).then(setDocs).catch(() => {})
  }

  useEffect(loadDocs, [])

  const upload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    setUploading(true)
    const form = new FormData()
    form.append('file', file)
    form.append('doc_type', 'document')
    try {
      await fetch('/api/documents/upload', { method: 'POST', body: form })
      loadDocs()
    } catch {}
    setUploading(false)
    e.target.value = ''
  }

  const deleteDoc = async (id: string) => {
    await fetch(`/api/documents/${id}`, { method: 'DELETE' })
    loadDocs()
  }

  return (
    <div style={{ padding: '1.5rem' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.5rem' }}>
        <h2>{config.ui_labels?.upload_button || 'Documents'}</h2>
        <label style={{ cursor: 'pointer' }}>
          <input type="file" onChange={upload} hidden
            accept=".pdf,.docx,.doc,.xlsx,.txt,.md,.csv" />
          <span style={{
            padding: '0.5rem 1rem', borderRadius: 6, fontSize: '0.85rem',
            background: 'var(--accent-primary)', color: 'white',
          }}>
            {uploading ? 'Uploading...' : (config.ui_labels?.upload_button || 'Upload')}
          </span>
        </label>
      </div>

      {docs.length === 0 ? (
        <p style={{ color: 'var(--text-muted)' }}>No documents yet. Upload files to build your knowledge base.</p>
      ) : (
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr style={{ borderBottom: '1px solid var(--glass-border)', textAlign: 'left' }}>
              <th style={{ padding: '0.5rem' }}>Filename</th>
              <th style={{ padding: '0.5rem' }}>Type</th>
              <th style={{ padding: '0.5rem' }}>Chunks</th>
              <th style={{ padding: '0.5rem' }}></th>
            </tr>
          </thead>
          <tbody>
            {docs.map(d => (
              <tr key={d.id} style={{ borderBottom: '1px solid var(--glass-border)' }}>
                <td style={{ padding: '0.5rem', fontSize: '0.85rem' }}>{d.filename}</td>
                <td style={{ padding: '0.5rem', fontSize: '0.85rem', color: 'var(--text-muted)' }}>{d.doc_type}</td>
                <td style={{ padding: '0.5rem', fontSize: '0.85rem' }}>{d.chunk_count}</td>
                <td style={{ padding: '0.5rem' }}>
                  <button onClick={() => deleteDoc(d.id)}
                    style={{ fontSize: '0.75rem', padding: '0.2rem 0.5rem', background: 'transparent' }}>
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
