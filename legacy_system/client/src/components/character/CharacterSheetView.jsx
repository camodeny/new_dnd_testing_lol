import Button from '../common/Button'

function Section({ title, children }) {
  return (
    <div className="sheet-section">
      <h3>{title}</h3>
      {children}
    </div>
  )
}

function Field({ label, value }) {
  if (value === undefined || value === null || value === '') return null
  return (
    <div className="sheet-field">
      <span className="sheet-label">{label}:</span>
      <span className="sheet-value">{typeof value === 'boolean' ? (value ? 'Yes' : 'No') : value}</span>
    </div>
  )
}

function Grid({ children, cols = 3 }) {
  return <div className={`sheet-grid cols-${cols}`}>{children}</div>
}

export default function CharacterSheetView({ character, onEdit, onDelete }) {
  if (!character) return null

  const c = character
  const abilityScores = c.ability_scores || {}
  const combat = c.combat || {}
  const general = c.general || {}
  const spellcasting = c.spellcasting || {}
  const currency = c.currency || {}
  const personality = c.personality || {}
  const appearance = c.appearance || {}
  const bg = c.background_details || {}

  return (
    <div className="character-sheet-view">
      <div className="sheet-header">
        <div>
          <h2>{c.name}</h2>
          <p className="sheet-subtitle">
            {c.race} {c.subrace ? `(${c.subrace})` : ''} &middot; Level {c.total_level} &middot; {c.alignment} &middot; {c.background}
          </p>
        </div>
        <div className="sheet-actions">
          {onEdit && <Button onClick={onEdit} variant="primary">Edit</Button>}
          {onDelete && <Button onClick={onDelete} variant="danger">Delete</Button>}
        </div>
      </div>

      <Section title="Ability Scores">
        <Grid cols={6}>
          {['strength', 'dexterity', 'constitution', 'intelligence', 'wisdom', 'charisma'].map((abl) => (
            <div key={abl} className="ability-box">
              <div className="ability-name">{abl.slice(0, 3).toUpperCase()}</div>
              <div className="ability-score">{abilityScores[abl]}</div>
              <div className="ability-mod">{Math.floor((abilityScores[abl] - 10) / 2)}</div>
            </div>
          ))}
        </Grid>
      </Section>

      <Section title="Combat">
        <Grid cols={4}>
          <Field label="Max HP" value={combat.max_hp} />
          <Field label="Current HP" value={combat.current_hp} />
          <Field label="Temp HP" value={combat.temp_hp} />
          <Field label="AC" value={combat.armor_class} />
          <Field label="Initiative" value={combat.initiative_bonus} />
          <Field label="Speed" value={combat.speed} />
          <Field label="Death Saves" value={`${combat.death_save_successes || 0} / ${combat.death_save_failures || 0}`} />
        </Grid>
      </Section>

      <Section title="General">
        <Grid cols={5}>
          <Field label="Proficiency Bonus" value={general.proficiency_bonus} />
          <Field label="Passive Perception" value={general.passive_perception} />
          <Field label="Exhaustion" value={general.exhaustion_level} />
          <Field label="Encumbrance" value={general.encumbrance_status} />
          <Field label="Inspiration" value={general.inspiration} />
        </Grid>
      </Section>

      {spellcasting.spellcasting_ability && (
        <Section title="Spellcasting">
          <Grid cols={3}>
            <Field label="Ability" value={spellcasting.spellcasting_ability} />
            <Field label="Save DC" value={spellcasting.spell_save_dc} />
            <Field label="Attack Bonus" value={spellcasting.spell_attack_bonus} />
          </Grid>
          {spellcasting.spell_slots && (
            <div className="spell-slots-grid">
              {Array.from({ length: 9 }, (_, i) => i + 1).map((lvl) => {
                const slot = spellcasting.spell_slots[lvl]
                if (!slot || slot.max === 0) return null
                return (
                  <div key={lvl} className="slot-pill">
                    <strong>Lvl {lvl}:</strong> {slot.max - (slot.used || 0)} / {slot.max}
                  </div>
                )
              })}
            </div>
          )}
        </Section>
      )}

      <Section title="Currency">
        <Grid cols={5}>
          <Field label="CP" value={currency.cp} />
          <Field label="SP" value={currency.sp} />
          <Field label="EP" value={currency.ep} />
          <Field label="GP" value={currency.gp} />
          <Field label="PP" value={currency.pp} />
        </Grid>
      </Section>

      <Section title="Personality">
        <Grid cols={2}>
          <Field label="Traits" value={personality.personality_traits} />
          <Field label="Ideals" value={personality.ideals} />
          <Field label="Bonds" value={personality.bonds} />
          <Field label="Flaws" value={personality.flaws} />
        </Grid>
      </Section>

      <Section title="Appearance">
        <Grid cols={3}>
          <Field label="Age" value={appearance.age} />
          <Field label="Height" value={appearance.height} />
          <Field label="Weight" value={appearance.weight} />
          <Field label="Eyes" value={appearance.eyes} />
          <Field label="Skin" value={appearance.skin} />
          <Field label="Hair" value={appearance.hair} />
        </Grid>
        <Field label="Description" value={appearance.character_appearance} />
      </Section>

      <Section title="Background">
        <Field label="Backstory" value={bg.backstory} />
        <Field label="Allies & Organizations" value={bg.allies_organizations} />
        <Field label="Additional Features" value={bg.additional_features_traits} />
        <Field label="Treasure" value={bg.treasure} />
      </Section>

      {c.classes?.length > 0 && (
        <Section title="Classes">
          <ul className="item-list-plain">
            {c.classes.map((cls, idx) => (
              <li key={idx}>
                <strong>{cls.class_name}</strong> {cls.subclass ? `(${cls.subclass})` : ''} — Level {cls.level} {cls.hit_die_type}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {c.skills?.length > 0 && (
        <Section title="Skills">
          <ul className="item-list-plain inline">
            {c.skills.map((s, idx) => (
              <li key={idx} className={s.is_expertise ? 'expertise' : s.is_proficient ? 'proficient' : ''}>
                {s.skill_name} {s.is_expertise ? '(E)' : s.is_proficient ? '(P)' : ''}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {c.saving_throws?.length > 0 && (
        <Section title="Saving Throws">
          <ul className="item-list-plain inline">
            {c.saving_throws.map((st, idx) => (
              <li key={idx} className={st.is_proficient ? 'proficient' : ''}>
                {st.ability} {st.is_proficient ? '(P)' : ''}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {c.proficiencies?.length > 0 && (
        <Section title="Proficiencies">
          <ul className="item-list-plain">
            {c.proficiencies.map((p, idx) => (
              <li key={idx}><strong>[{p.proficiency_type}]</strong> {p.name} {p.notes ? `— ${p.notes}` : ''}</li>
            ))}
          </ul>
        </Section>
      )}

      {c.features?.length > 0 && (
        <Section title="Features & Traits">
          <ul className="item-list-plain">
            {c.features.map((f, idx) => (
              <li key={idx}>
                <strong>{f.name}</strong> {f.source ? `(${f.source})` : ''}
                {f.uses_max !== null && <span className="uses"> — {f.uses_current ?? 0}/{f.uses_max} {f.recharge ? `(${f.recharge})` : ''}</span>}
                {f.description && <div className="item-desc">{f.description}</div>}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {c.weapons?.length > 0 && (
        <Section title="Weapons">
          <ul className="item-list-plain">
            {c.weapons.map((w, idx) => (
              <li key={idx} className={w.is_equipped ? 'equipped' : ''}>
                <strong>{w.name}</strong> {w.is_equipped ? '(Equipped)' : ''} — Attack +{w.attack_bonus}, {w.damage} {w.damage_type}
                {w.properties && <span className="item-meta"> — {w.properties}</span>}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {c.equipment?.length > 0 && (
        <Section title="Equipment">
          <ul className="item-list-plain">
            {c.equipment.map((e, idx) => (
              <li key={idx} className={e.is_equipped ? 'equipped' : ''}>
                <strong>{e.name}</strong> {e.is_equipped ? '(Equipped)' : ''} x{e.quantity}
                {e.equipment_type && <span className="item-meta"> — {e.equipment_type}</span>}
                {e.description && <div className="item-desc">{e.description}</div>}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {c.spells?.length > 0 && (
        <Section title="Spells">
          <ul className="item-list-plain">
            {c.spells.map((s, idx) => (
              <li key={idx} className={s.is_prepared ? 'prepared' : ''}>
                <strong>{s.name}</strong> {s.is_ritual ? '(Ritual)' : ''} {s.is_concentration ? '(Concentration)' : ''} {s.is_prepared ? '(Prepared)' : ''}
                <span className="item-meta"> — Lvl {s.spell_level}, {s.school}, {s.casting_time}, {s.range}</span>
                {s.description && <div className="item-desc">{s.description}</div>}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {c.resources?.length > 0 && (
        <Section title="Resources">
          <ul className="item-list-plain">
            {c.resources.map((r, idx) => (
              <li key={idx}>
                <strong>{r.name}</strong> — {r.current}/{r.max} {r.recharge ? `(${r.recharge})` : ''}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {c.companions?.length > 0 && (
        <Section title="Companions">
          <ul className="item-list-plain">
            {c.companions.map((comp, idx) => (
              <li key={idx}>
                <strong>{comp.name}</strong> {comp.companion_type ? `(${comp.companion_type})` : ''} — HP {comp.current_hp}/{comp.max_hp}, AC {comp.armor_class}
                {comp.description && <div className="item-desc">{comp.description}</div>}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {c.conditions?.length > 0 && (
        <Section title="Conditions">
          <ul className="item-list-plain">
            {c.conditions.map((cond, idx) => (
              <li key={idx} className={cond.is_permanent ? 'permanent' : ''}>
                <strong>{cond.condition_name}</strong> {cond.is_permanent ? '(Permanent)' : cond.duration_remaining ? `(${cond.duration_remaining})` : ''}
                {cond.source && <span className="item-meta"> — Source: {cond.source}</span>}
                {cond.description && <div className="item-desc">{cond.description}</div>}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {c.notes?.length > 0 && (
        <Section title="Notes">
          <ul className="item-list-plain">
            {c.notes.map((n, idx) => (
              <li key={idx}>
                <strong>{n.title || 'Untitled'}</strong>
                <div className="item-desc">{n.content}</div>
              </li>
            ))}
          </ul>
        </Section>
      )}
    </div>
  )
}
