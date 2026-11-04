import type { ReactElement } from 'react'
import { useState } from 'react'
import { adminApi } from '../../admin/api'
import type { AdminFeature } from '../../admin/api'
import { attempt, useLoad } from '../../admin/useLoad'
import { ListSkeleton } from '../../components/AdminSkeletons'
import { Alert, Badge, Button, Card, Field, Input, Modal, PageHeader, Toggle } from '../../components/ui'

export default function Features(): ReactElement {
  const features = useLoad(() => adminApi.features())
  const [message, setMessage] = useState<{ tone: 'error' | 'success'; text: string } | null>(null)
  const [editing, setEditing] = useState<AdminFeature | null>(null)
  const [form, setForm] = useState({ name: '', description: '' })

  async function save(key: string, body: Parameters<typeof adminApi.updateFeature>[1], success: string): Promise<void> {
    setMessage(null)
    if (await attempt(() => adminApi.updateFeature(key, body), (t) => setMessage({ tone: 'error', text: t }))) {
      setMessage({ tone: 'success', text: success })
      await features.reload()
    }
  }

  return (
    <div>
      <PageHeader
        title="Features"
        subtitle="The tools Rejecta offers. Switching one off here disables it for every plan and partner at once (for outages or maintenance)."
      />
      {message ? <div className="mb-4"><Alert tone={message.tone}>{message.text}</Alert></div> : null}
      <Alert>{features.error}</Alert>
      {features.loading && !features.data ? <ListSkeleton count={5} label="Loading features" /> : null}
      <div className="flex flex-col gap-3">
        {(features.data ?? []).map((f) => (
          <Card key={f.key}>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="min-w-0">
                <p className="flex items-center gap-2 font-medium text-gray-900">
                  {f.name} {f.globally_enabled ? <Badge tone="green">on</Badge> : <Badge tone="red">off for everyone</Badge>}
                </p>
                <p className="font-mono text-xs text-gray-500">{f.key}</p>
                <p className="mt-1 text-sm text-gray-600">{f.description}</p>
              </div>
              <div className="flex items-center gap-3">
                <Button variant="secondary" onClick={() => { setEditing(f); setForm({ name: f.name, description: f.description }) }}>Edit</Button>
                <Toggle
                  checked={f.globally_enabled}
                  label={`${f.name} available`}
                  onChange={(next) => {
                    if (!next && !window.confirm(`Turn ${f.name} off for everyone, on every plan?`)) return
                    void save(f.key, { globally_enabled: next }, `${f.name} turned ${next ? 'on' : 'off'}.`)
                  }}
                />
              </div>
            </div>
          </Card>
        ))}
      </div>
      <Modal open={editing !== null} title={`Edit ${editing?.name ?? ''}`} onClose={() => setEditing(null)}
        footer={<><Button variant="secondary" onClick={() => setEditing(null)}>Cancel</Button>
          <Button disabled={!form.name.trim()} onClick={() => {
            if (editing) void save(editing.key, form, 'Feature updated.').then(() => setEditing(null))
          }}>Save</Button></>}>
        <Field label="Name" htmlFor="f-name"><Input id="f-name" value={form.name} maxLength={80} onChange={(e) => setForm({ ...form, name: e.target.value })} /></Field>
        <Field label="Description" htmlFor="f-desc"><Input id="f-desc" value={form.description} maxLength={500} onChange={(e) => setForm({ ...form, description: e.target.value })} /></Field>
        <p className="mt-3 text-xs text-gray-500">The key is fixed: it is how the API knows which tool this is.</p>
      </Modal>
    </div>
  )
}
