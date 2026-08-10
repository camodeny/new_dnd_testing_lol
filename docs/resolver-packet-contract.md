# Resolver packet contract

`talk_to_player.resolver_packet` is an optional, DM-private identity sidecar. It is never rendered into the player-visible response. The runtime schema is defined once as `RESOLVER_PACKET_SCHEMA` in `server/services/memory_resolver_schemas.py`; the finalizer tool declaration, parser, accepted-turn persistence path, and session-memory compiler all use that contract.

Omit `resolver_packet` when the turn makes no deliberate identity commitment. When present, it has this shape:

```json
{
  "entity_mentions": [
    {
      "mention_ref": "hooded_figure_1",
      "surface_form": "the hooded figure",
      "identity_status": "known_hidden",
      "visibility": "dm_private",
      "canonical_id": "harlen_moss",
      "public_name": "the hooded figure",
      "evidence_refs": ["campaign_entity:harlen_moss"]
    }
  ]
}
```

Every mention requires `mention_ref`, `surface_form`, and `identity_status`. `mention_ref` must begin with a letter and contain only letters, digits, and underscores. `identity_status` is one of `known_hidden`, `known_public`, `intentionally_undetermined`, `provisional_new_entity`, `provisional_unknown`, or `candidate_existing_entity`. Optional `visibility` is one of `public`, `party_known`, or `dm_private`. Optional `canonical_id`, `public_name`, and `evidence_refs` may be null. A non-null `canonical_id` is a durable identity commitment and requires at least one `evidence_refs` entry. Undeclared aliases such as `entity`, `mention`, `name`, `role`, and `campaign_entity` are invalid.

The finalizer parser validates this sidecar before constructing an accepted turn. A malformed packet triggers at most two resolver-contract-only repair calls. Those calls must preserve the original response parts and staged action IDs exactly. If repair remains malformed and the packet contains no canonical identity commitment, the runtime omits the optional packet and continues with the preserved visible turn. If a canonical identity commitment was attempted, the turn fails closed before staged actions, response parts, the visible message, or a resolver row can be persisted.

Resolver repairs use separate audit events: `resolver_contract_repair_requested`, `resolver_contract_repair_completed`, `resolver_contract_packet_omitted`, and `resolver_contract_repair_blocked`. Automation retry metrics classify requested repairs as `resolver_contract_repair`, separately from narrative guard and general finalizer-contract retries.
