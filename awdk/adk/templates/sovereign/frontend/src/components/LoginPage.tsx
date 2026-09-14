export default function LoginPage({ auth }: { auth: any }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      minHeight: '100vh', background: 'var(--bg-deep)',
    }}>
      <div style={{
        background: 'var(--bg-base)', border: '1px solid var(--glass-border)',
        borderRadius: 12, padding: '2rem', width: 360, textAlign: 'center',
      }}>
        <h2 style={{ marginBottom: '0.5rem' }}>Welcome</h2>
        <p style={{ color: 'var(--text-muted)', marginBottom: '1.5rem', fontSize: '0.85rem' }}>
          Sign in to get started.
        </p>
        <button onClick={auth.login} style={{
          width: '100%', padding: '0.75rem', background: 'var(--accent-primary)',
          border: 'none', borderRadius: 8, fontWeight: 600,
        }}>
          Dev Login
        </button>
        <p style={{ marginTop: '1rem', fontSize: '0.75rem', color: 'var(--text-muted)' }}>
          In production, this uses OIDC via AitherIdentity.
        </p>
      </div>
    </div>
  )
}
