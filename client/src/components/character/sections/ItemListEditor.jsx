import { useState } from 'react'
import Button from '../../common/Button'
import Input from '../../common/Input'
import NumberInput from '../../common/NumberInput'
import TextArea from '../../common/TextArea'

export default function ItemListEditor({ title, items, onChange, fields, emptyItem }) {
  const singularTitle = {
    Classes: 'Class',
    Proficiencies: 'Proficiency',
  }[title] || title.replace(/s$/, '')
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

  const renderItemDisplay = (item, idx) => {
    const listKey = title.toLowerCase()

    const renderBadge = (label, type) => (
      <span className={`item-badge ${type}`} key={label}>{label}</span>
    )

    let content

    switch (listKey) {
      case 'classes':
        content = (
          <div className="item-display-content">
            <div className="item-display-header">
              <span className="item-name">{item.class_name || 'Unnamed Class'}</span>
              {renderBadge(`Lvl ${item.level || 1}`, 'prepared')}
            </div>
            <div className="item-details-grid">
              {item.subclass && <span className="item-detail-tag">Subclass: {item.subclass}</span>}
              {item.hit_die_type && <span className="item-detail-tag">Hit Die: {item.hit_die_type}</span>}
            </div>
          </div>
        )
        break

      case 'skills':
        content = (
          <div className="item-display-content">
            <div className="item-display-header">
              <span className="item-name">{item.skill_name || 'Unnamed Skill'}</span>
              {item.is_proficient && renderBadge('Proficient', 'proficient')}
              {item.is_expertise && renderBadge('Expertise', 'expertise')}
            </div>
            {item.bonus_override !== null && item.bonus_override !== undefined && (
              <div className="item-details-grid">
                <span className="item-detail-tag">Bonus: {item.bonus_override >= 0 ? `+${item.bonus_override}` : item.bonus_override}</span>
              </div>
            )}
          </div>
        )
        break

      case 'saving throws':
        content = (
          <div className="item-display-content">
            <div className="item-display-header">
              <span className="item-name">{item.ability || 'Unnamed Ability'}</span>
              {item.is_proficient && renderBadge('Proficient', 'proficient')}
            </div>
            {item.bonus_override !== null && item.bonus_override !== undefined && (
              <div className="item-details-grid">
                <span className="item-detail-tag">Bonus: {item.bonus_override >= 0 ? `+${item.bonus_override}` : item.bonus_override}</span>
              </div>
            )}
          </div>
        )
        break

      case 'proficiencies':
        content = (
          <div className="item-display-content">
            <div className="item-display-header">
              <span className="item-name">{item.name || 'Unnamed'}</span>
              {item.proficiency_type && renderBadge(item.proficiency_type, 'prepared')}
            </div>
            {item.notes && <div className="item-desc">{item.notes}</div>}
          </div>
        )
        break

      case 'features':
        content = (
          <div className="item-display-content">
            <div className="item-display-header">
              <span className="item-name">{item.name || 'Unnamed Feature'}</span>
              {item.source && renderBadge(item.source, 'prepared')}
              {item.recharge && renderBadge(item.recharge, 'concentration')}
            </div>
            {item.uses_max !== null && item.uses_max !== undefined && (
              <div className="item-details-grid">
                <span className="item-detail-tag">Uses: {item.uses_current ?? 0} / {item.uses_max}</span>
              </div>
            )}
            {item.description && <div className="item-desc">{item.description}</div>}
          </div>
        )
        break

      case 'weapons':
        content = (
          <div className="item-display-content">
            <div className="item-display-header">
              <span className="item-name">{item.name || 'Unnamed Weapon'}</span>
              {item.is_equipped && renderBadge('Equipped', 'equipped')}
              {item.attack_bonus !== undefined && item.attack_bonus !== null && renderBadge(`Atk: ${item.attack_bonus >= 0 ? `+${item.attack_bonus}` : item.attack_bonus}`, 'prepared')}
            </div>
            <div className="item-details-grid">
              {item.damage && <span className="item-detail-tag">Dmg: {item.damage} {item.damage_type || ''}</span>}
              {item.properties && <span className="item-detail-tag">Props: {item.properties}</span>}
            </div>
            {item.notes && <div className="item-desc">{item.notes}</div>}
          </div>
        )
        break

      case 'equipment':
        content = (
          <div className="item-display-content">
            <div className="item-display-header">
              <span className="item-name">{item.name || 'Unnamed Item'}</span>
              {item.is_equipped && renderBadge('Equipped', 'equipped')}
              {item.quantity !== undefined && renderBadge(`Qty: ${item.quantity}`, 'ritual')}
            </div>
            <div className="item-details-grid">
              {item.equipment_type && <span className="item-detail-tag">Type: {item.equipment_type}</span>}
              {item.weight !== null && item.weight !== undefined && <span className="item-detail-tag">Weight: {item.weight} lbs</span>}
              {item.armor_bonus !== null && item.armor_bonus !== undefined && <span className="item-detail-tag">AC Bonus: +{item.armor_bonus}</span>}
              {item.properties && <span className="item-detail-tag">Props: {item.properties}</span>}
            </div>
            {item.description && <div className="item-desc">{item.description}</div>}
          </div>
        )
        break

      case 'spells':
        content = (
          <div className="item-display-content">
            <div className="item-display-header">
              <span className="item-name">{item.name || 'Unnamed Spell'}</span>
              {item.is_prepared && renderBadge('Prepared', 'prepared')}
              {item.is_ritual && renderBadge('Ritual', 'ritual')}
              {item.is_concentration && renderBadge('Concentration', 'concentration')}
            </div>
            <div className="item-details-grid">
              <span className="item-detail-tag">Level: {item.spell_level ?? 0}</span>
              {item.school && <span className="item-detail-tag">{item.school}</span>}
              {item.casting_time && <span className="item-detail-tag">{item.casting_time}</span>}
              {item.range && <span className="item-detail-tag">Range: {item.range}</span>}
              {item.duration && <span className="item-detail-tag">{item.duration}</span>}
            </div>
            {item.description && <div className="item-desc">{item.description}</div>}
          </div>
        )
        break

      case 'resources':
        content = (
          <div className="item-display-content">
            <div className="item-display-header">
              <span className="item-name">{item.name || 'Unnamed Resource'}</span>
              {item.recharge && renderBadge(item.recharge, 'concentration')}
            </div>
            <div className="item-details-grid">
              <span className="item-detail-tag">Value: {item.current ?? 0} / {item.max ?? 0}</span>
            </div>
          </div>
        )
        break

      case 'companions':
        content = (
          <div className="item-display-content">
            <div className="item-display-header">
              <span className="item-name">{item.name || 'Unnamed Companion'}</span>
              {item.companion_type && renderBadge(item.companion_type, 'prepared')}
            </div>
            <div className="item-details-grid">
              <span className="item-detail-tag">HP: {item.current_hp ?? 0} / {item.max_hp ?? 1}</span>
              {item.armor_class !== null && item.armor_class !== undefined && <span className="item-detail-tag">AC: {item.armor_class}</span>}
              {item.speed && <span className="item-detail-tag">Speed: {item.speed}</span>}
            </div>
            {item.description && <div className="item-desc">{item.description}</div>}
          </div>
        )
        break

      case 'conditions':
        content = (
          <div className="item-display-content">
            <div className="item-display-header">
              <span className="item-name">{item.condition_name || 'Unnamed Condition'}</span>
              {item.is_permanent && renderBadge('Permanent', 'equipped')}
              {item.source && renderBadge(item.source, 'prepared')}
            </div>
            {item.duration_remaining && (
              <div className="item-details-grid">
                <span className="item-detail-tag">Duration Left: {item.duration_remaining}</span>
              </div>
            )}
            {item.description && <div className="item-desc">{item.description}</div>}
          </div>
        )
        break

      default:
        content = (
          <div className="item-display-content">
            <span className="item-name">{getItemLabel(item)}</span>
          </div>
        )
        break
    }

    return (
      <div className="item-display">
        {content}
        <div className="item-actions">
          <Button onClick={() => startEdit(idx)} variant="secondary" className="small">Edit</Button>
          <Button onClick={() => remove(idx)} variant="danger" className="small">Remove</Button>
        </div>
      </div>
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
                  <div key={f.key} className={`item-field ${f.type === 'textarea' ? 'full-width' : ''}`}>
                    {f.type !== 'checkbox' && <label>{f.label}</label>}
                    {renderField(f, draft[f.key], (key, val) => setDraft({ ...draft, [key]: val }))}
                  </div>
                ))}
                <div className="item-form-actions">
                  <Button onClick={save} variant="primary">Save</Button>
                  <Button onClick={cancel} variant="secondary">Cancel</Button>
                </div>
              </div>
            ) : (
              renderItemDisplay(item, idx)
            )}
          </li>
        ))}
      </ul>
      {editingIndex === -1 ? (
        <div className="item-form">
          {fields.map((f) => (
            <div key={f.key} className={`item-field ${f.type === 'textarea' ? 'full-width' : ''}`}>
              {f.type !== 'checkbox' && <label>{f.label}</label>}
              {renderField(f, draft[f.key], (key, val) => setDraft({ ...draft, [key]: val }))}
            </div>
          ))}
          <div className="item-form-actions">
            <Button onClick={save} variant="primary">Save</Button>
            <Button onClick={cancel} variant="secondary">Cancel</Button>
          </div>
        </div>
      ) : (
        <Button onClick={startAdd} variant="primary" className="add-btn">Add {singularTitle}</Button>
      )}
    </div>
  )
}
