"""World / memory / knowledge domain.

Owns authoritative campaign world state: canonical world entities and the
current-scene row (issue #209). Reads are viewer-aware (restricted
visibility is owner-only); there are no legacy aggregate/map stubs.
"""

