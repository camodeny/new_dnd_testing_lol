export const mockCharacters = [
  {
    id: 'e2e-character',
    user_id: 1,
    name: 'E2E Mocked Character',
    race: 'Elf',
    alignment: 'Chaotic Good',
    total_level: 5,
    combat: { current_hp: 42, max_hp: 42, temp_hp: 5, armor_class: 16, initiative_bonus: 2 },
    ability_scores: {
      strength: 10,
      dexterity: 18,
      constitution: 14,
      intelligence: 12,
      wisdom: 16,
      charisma: 8,
    },
    classes: [
      { class_name: 'Ranger', level: 5 }
    ],
    conditions: [
      { id: 'c1', condition_name: 'Concentration', description: 'Concentrating on Hunter\'s Mark' },
      { id: 'c2', condition_name: 'Poisoned', description: 'Affected by spider venom' }
    ]
  }
];
