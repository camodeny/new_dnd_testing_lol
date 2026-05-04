import { useState } from 'react'
import Button from '../../common/Button'
import Input from '../../common/Input'
import NumberInput from '../../common/NumberInput'
import TextArea from '../../common/TextArea'

export default function ItemListEditor({ title, items, onChange, fields, emptyItem }) {
  const [editingIndex, setEditingIndex] = useState(null)
  const [draft, setDraft] = useState(null)

  const startAdd = () => {
    setDraft({ ...emptyItem })
    setEditingIndex(-1)
  }

  const startEdit = (index) => {
    setDraft({ ...items[index] })
    setEditingIndex(index)
  }

  const cancel = () => {
    setDraft(null)
    setEditingIndex(null)
  }

  const save = () => {
    const next = [...items]
    if (editingIndex === -1) {
      next.push(draft)
    } else {
      next[editingIndex] = draft
    }
    onChange(next)
    setDraft(null)
    setEditingIndex(null)
  }

  const remove = (index) => {
    const next = [...items]
    next.splice(index, 1)
    onChange(next)
  }

  const getItemLabel = (item) => {
    if (typeof item === 'string') return item
    if (!item || typeof item !== 'object') return 'Untitled'
    const labelField = fields.find((field) => {
      if (field.type === 'checkbox') return false
      const value = item[field.key]
      return value !== undefined && value !== null && String(value).trim() !== ''
    })
    return labelField ? item[labelField.key] : 'Untitled'
  }

  const renderField = (field, value, onFieldChange) => {
    if (field.type === 'textarea') {
      return (
        <TextArea
          value={value || ''}
          onChange={(e) => onFieldChange(field.key, e.target.value)}
          placeholder={field.label}
          rows={2}
        />
      )
    }
    if (field.type === 'number') {
      return (
        <NumberInput
          value={value}
          onChange={(v) => onFieldChange(field.key, v)}
          placeholder={field.label}
        />
      )
    }
    if (field.type === 'checkbox') {
      return (
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={!!value}
            onChange={(e) => onFieldChange(field.key, e.target.checked)}
          />
          {field.label}
        </label>
      )
    }
    return (
      <Input
        type={field.type || 'text'}
        value={value || ''}
        onChange={(e) => onFieldChange(field.key, e.target.value)}
        placeholder={field.label}
      />
    )
  }

  return (
    <div className="item-list-editor">
      <h4>{title}</h4>
      {items.length === 0 && <p className="empty-list">No {title.toLowerCase()} yet.</p>}
      <ul className="item-list">
        {items.map((item, idx) => (
          <li key={idx} className="item-row">
            {editingIndex === idx ? (
              <div className="item-form">
                {fields.map((f) => (
                  <div key={f.key} className="item-field">
                    {f.type !== 'checkbox' && <label>{f.label}</label>}
                    {renderField(f, draft[f.key], (key, val) => setDraft({ ...draft, [key]: val }))}
                  </div>
                ))}
                <div className="item-actions">
                  <Button onClick={save} variant="primary">Save</Button>
                  <Button onClick={cancel} variant="secondary">Cancel</Button>
                </div>
              </div>
            ) : (
              <div className="item-display">
                <span className="item-name">{getItemLabel(item)}</span>
                <div className="item-actions">
                  <Button onClick={() => startEdit(idx)} variant="secondary" className="small">Edit</Button>
                  <Button onClick={() => remove(idx)} variant="danger" className="small">Remove</Button>
                </div>
              </div>
            )}
          </li>
        ))}
      </ul>
      {editingIndex === -1 ? (
        <div className="item-form">
          {fields.map((f) => (
            <div key={f.key} className="item-field">
              {f.type !== 'checkbox' && <label>{f.label}</label>}
              {renderField(f, draft[f.key], (key, val) => setDraft({ ...draft, [key]: val }))}
            </div>
          ))}
          <div className="item-actions">
            <Button onClick={save} variant="primary">Save</Button>
            <Button onClick={cancel} variant="secondary">Cancel</Button>
          </div>
        </div>
      ) : (
        <Button onClick={startAdd} variant="primary" className="add-btn">Add {title.replace(/s$/, '')}</Button>
      )}
    </div>
  )
}
