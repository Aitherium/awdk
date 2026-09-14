import { useState, useEffect } from 'react'

interface EmbedConfig {
  loaded: boolean
  app_name: string
  company_name: string
  welcome_message: string
  features: string[]
  doc_types: { id: string; label: string; extract: boolean }[]
  ui_labels: Record<string, string>
  roles: Record<string, { description: string; permissions: string[] }>
}

export function useConfig(): EmbedConfig {
  const [config, setConfig] = useState<EmbedConfig>({
    loaded: false,
    app_name: 'Agent',
    company_name: '',
    welcome_message: '',
    features: [],
    doc_types: [],
    ui_labels: {},
    roles: {},
  })

  useEffect(() => {
    fetch('/api/config/embed')
      .then(r => r.json())
      .then(data => setConfig({ ...data, loaded: true }))
      .catch(() => setConfig(prev => ({ ...prev, loaded: true })))
  }, [])

  return config
}
