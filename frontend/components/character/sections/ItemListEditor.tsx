import { useState } from 'react'
import Button from '@/components/common/Button'
import Input from '@/components/common/Input'
import NumberInput from '@/components/common/NumberInput'
import TextArea from '@/components/common/TextArea'
import type { FormValue, ItemFieldConfig, ItemRecord } from '../characterFormConfig'

interface Props {
  title: string
  items: ItemRecord[]
  fields: ItemFieldConfig[]
  emptyItem: ItemRecord
  onChange: (items: ItemRecord[]) => void
}

const SINGULAR_TITLES: Record<string, string> = {
  Classes: 'Class',
  Proficiencies: 'Proficiency',
}

function itemLabel(item: ItemRecord, fields: ItemFieldConfig[]): string {
  const field = fields.find(({ key, type }) => type !== 'checkbox' && String(item[key] ?? '').trim())
  return field ? String(item[field.key]) : 'Untitled'
}

function fieldValueLabel(field: ItemFieldConfig, value: FormValue): string | null {
  if (field.type === 'checkbox') return value ? field.label : null
  if (value === null || value === '' || value === undefined) return null
  return `${field.label}: ${String(value)}`
}

export default function ItemListEditor({ title, items, fields, emptyItem, onChange }: Props) {
  const [editingIndex, setEditingIndex] = useState<number | null>(null)
  const [draft, setDraft] = useState<ItemRecord | null>(null)
  const singularTitle = SINGULAR_TITLES[title] ?? title.replace(/s$/, '')

  const startAdd = () => {
    setDraft({ ...emptyItem })
    setEditingIndex(-1)
  }

  const startEdit = (index: number) => {
    setDraft({ ...items[index] })
    setEditingIndex(index)
  }

  const cancel = () => {
    setDraft(null)
    setEditingIndex(null)
  }

  const save = () => {
    if (!draft || editingIndex === null) return
    const next = [...items]
    if (editingIndex === -1) next.push(draft)
    else next[editingIndex] = draft
    onChange(next)
    cancel()
  }

  const remove = (index: number) => onChange(items.filter((_, itemIndex) => itemIndex !== index))

  const renderField = (field: ItemFieldConfig) => {
    if (!draft) return null
    const update = (value: FormValue) => setDraft({ ...draft, [field.key]: value })
    if (field.type === 'textarea') {
      return <TextArea value={String(draft[field.key] ?? '')} onChange={(event) => update(event.target.value)} rows={2} />
    }
    if (field.type === 'number') {
      const value = draft[field.key]
      return <NumberInput value={typeof value === 'number' ? value : null} onChange={update} />
    }
    if (field.type === 'checkbox') {
      return (
        <label className="checkbox-label">
          <input type="checkbox" checked={Boolean(draft[field.key])} onChange={(event) => update(event.target.checked)} />
          {field.label}
        </label>
      )
    }
    return <Input value={String(draft[field.key] ?? '')} onChange={(event) => update(event.target.value)} />
  }

  const editor = (
    <div className="item-form">
      {fields.map((field) => (
        <div key={field.key} className={`item-field${field.type === 'textarea' ? ' full-width' : ''}`}>
          {field.type !== 'checkbox' && <label>{field.label}</label>}
          {renderField(field)}
        </div>
      ))}
      <div className="item-form-actions">
        <Button type="button" variant="primary" onClick={save}>Save</Button>
        <Button type="button" variant="secondary" onClick={cancel}>Cancel</Button>
      </div>
    </div>
  )

  return (
    <section className="item-list-editor">
      <h4>{title}</h4>
      {items.length === 0 && editingIndex !== -1 && <p className="empty-list">No {title.toLowerCase()} yet.</p>}
      <ul className="item-list">
        {items.map((item, index) => (
          <li key={index} className="item-row">
            {editingIndex === index ? editor : (
              <div className="item-display">
                <div className="item-display-content">
                  <div className="item-display-header">
                    <span className="item-name">{itemLabel(item, fields)}</span>
                  </div>
                  <div className="item-details-grid">
                    {fields.slice(1).map((field) => {
                      const label = fieldValueLabel(field, item[field.key])
                      return label ? <span key={field.key} className="item-detail-tag">{label}</span> : null
                    })}
                  </div>
                </div>
                <div className="item-actions">
                  <Button type="button" size="small" onClick={() => startEdit(index)}>Edit</Button>
                  <Button type="button" size="small" variant="danger" onClick={() => remove(index)}>Remove</Button>
                </div>
              </div>
            )}
          </li>
        ))}
      </ul>
      {editingIndex === -1 ? editor : (
        <Button type="button" variant="primary" className="add-btn" onClick={startAdd}>
          Add {singularTitle}
        </Button>
      )}
    </section>
  )
}
