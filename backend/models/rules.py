"""Rules corpus and embeddings domain models — independent of campaign/world-memory tables."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class RulesCorpus(Base):
    """One versioned rules corpus build (e.g. dnd-srd / 5.2.1)."""

    __tablename__ = "rules_corpora"

    corpus_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    corpus_version: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_url: Mapped[str] = mapped_column(String(512), nullable=False)
    source_checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_artifact_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    license: Mapped[str] = mapped_column(String(64), nullable=False, default="CC BY 4.0", server_default="CC BY 4.0")
    attribution: Mapped[str | None] = mapped_column(Text, nullable=True)
    import_build_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    pinned_inputs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def to_dict(self):
        return {
            "corpus_id": self.corpus_id,
            "corpus_version": self.corpus_version,
            "source_url": self.source_url,
            "source_checksum": self.source_checksum,
            "source_artifact_hash": self.source_artifact_hash,
            "license": self.license,
            "attribution": self.attribution,
            "import_build_id": self.import_build_id,
            "imported_at": self.imported_at.isoformat() if self.imported_at else None,
            "pinned_inputs": self.pinned_inputs,
        }


class RulesSection(Base):
    """Canonical rule/section record — stable citation identity."""

    __tablename__ = "rules_sections"
    __table_args__ = (
        UniqueConstraint("corpus_id", "corpus_version", "source_section_id", name="uq_rules_sections_corpus_source"),
        Index("ix_rules_sections_corpus", "corpus_id", "corpus_version"),
        Index("ix_rules_sections_source_section_id", "source_section_id"),
    )

    rule_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    corpus_id: Mapped[str] = mapped_column(String(64), nullable=False)
    corpus_version: Mapped[str] = mapped_column(String(32), nullable=False)
    source_section_id: Mapped[str] = mapped_column(String(256), nullable=False)
    source_locator: Mapped[str] = mapped_column(String(256), nullable=False)
    document: Mapped[str] = mapped_column(String(256), nullable=False)
    heading_path: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'"))
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    structured_tables: Mapped[list | dict | None] = mapped_column(JSONB, nullable=True)
    source_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    citation_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    import_build_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def citation(self) -> dict:
        meta = self.citation_metadata or {}
        return {
            "rule_id": self.rule_id,
            "corpus_id": self.corpus_id,
            "corpus_version": self.corpus_version,
            "title": self.title,
            "path": "/".join(self.heading_path) if isinstance(self.heading_path, list) else str(self.heading_path),
            "heading_path": self.heading_path,
            "document": self.document,
            "source_locator": self.source_locator,
            "source_section_id": self.source_section_id,
            "source_url": meta.get("source_url"),
            "license": meta.get("license", "CC BY 4.0"),
            "attribution": meta.get("attribution"),
        }

    def to_dict(self, *, include_body: bool = True):
        d = {
            "rule_id": self.rule_id,
            "corpus_id": self.corpus_id,
            "corpus_version": self.corpus_version,
            "source_section_id": self.source_section_id,
            "source_locator": self.source_locator,
            "document": self.document,
            "heading_path": self.heading_path,
            "title": self.title,
            "structured_tables": self.structured_tables,
            "source_hash": self.source_hash,
            "content_hash": self.content_hash,
            "citation_metadata": self.citation_metadata,
            "import_build_id": self.import_build_id,
            "citation": self.citation(),
        }
        if include_body:
            d["body"] = self.body
        else:
            d["excerpt"] = self.body[:600] if self.body else ""
        return d


class RulesSectionAlias(Base):
    __tablename__ = "rules_section_aliases"

    alias: Mapped[str] = mapped_column(String(256), primary_key=True)
    corpus_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    corpus_version: Mapped[str] = mapped_column(String(32), primary_key=True)
    rule_id: Mapped[str] = mapped_column(String(256), ForeignKey("rules_sections.rule_id", ondelete="CASCADE"), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)


class RulesEmbedding(Base):
    """Rebuildable derived vector index — not canonical storage."""

    __tablename__ = "rules_embeddings"

    rule_id: Mapped[str] = mapped_column(String(256), ForeignKey("rules_sections.rule_id", ondelete="CASCADE"), primary_key=True)
    corpus_id: Mapped[str] = mapped_column(String(64), nullable=False)
    corpus_version: Mapped[str] = mapped_column(String(32), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(64), primary_key=True)
    embedding_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1", server_default="1")
    build_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    embedding: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_strategy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RulesCorpusImport(Base):
    __tablename__ = "rules_corpus_imports"

    build_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    corpus_id: Mapped[str] = mapped_column(String(64), nullable=False)
    corpus_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    source_checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pinned_inputs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    validation_errors: Mapped[list | dict | None] = mapped_column(JSONB, nullable=True)
    canary_results: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
