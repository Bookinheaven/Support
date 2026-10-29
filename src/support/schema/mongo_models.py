"""MongoDB schema models for Support.

Represents document collection structure discovered via bounded document sampling.
Unlike relational schemas with rigid DDL, MongoDB documents within a collection can
exhibit heterogeneous structures, optional fields, and polymorphic types.
"""

from typing import Any, Optional
from pydantic import BaseModel, Field


class MongoFieldSchema(BaseModel):
    """Schema metadata for a single field in a collection inferred from sampled documents."""

    name: str = Field(..., description="Field path or name (e.g. 'title' or 'address.city')")
    types: list[str] = Field(default_factory=list, description="Observed BSON types (e.g. ['string', 'int', 'null'])")
    count: int = Field(default=0, description="Number of sampled documents where this field was present")
    sample_size: int = Field(default=0, description="Total number of documents sampled in this collection")
    nullable: bool = Field(default=False, description="True if the field was observed as null or absent in some sampled documents")
    has_nested: bool = Field(default=False, description="True if this field contains nested documents or arrays")

    @property
    def occurrence_rate(self) -> float:
        """Percentage of sampled documents containing this field."""
        if self.sample_size == 0:
            return 0.0
        return round((self.count / self.sample_size) * 100, 1)

    @property
    def type_display(self) -> str:
        """Display string of types, highlighting polymorphic fields."""
        if not self.types:
            return "unknown"
        if len(self.types) == 1:
            return self.types[0]
        return " | ".join(self.types)


class MongoIndexSchema(BaseModel):
    """Index metadata for a collection."""

    name: str = Field(..., description="Index name")
    keys: list[tuple[str, Any]] = Field(default_factory=list, description="Key specifications, e.g. [('email', 1)]")
    unique: bool = Field(default=False, description="True if this is a unique index")


class MongoCollectionSchema(BaseModel):
    """Schema metadata for a MongoDB collection."""

    name: str = Field(..., description="Collection name")
    estimated_count: Optional[int] = Field(default=None, description="Estimated total document count in collection")
    sample_size: int = Field(default=0, description="Actual number of documents sampled for inspection")
    is_empty: bool = Field(default=False, description="True if the collection contains zero documents")
    fields: list[MongoFieldSchema] = Field(default_factory=list, description="Sampled field schemas")
    indexes: list[MongoIndexSchema] = Field(default_factory=list, description="Collection indexes")

    @property
    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]


class MongoDatabaseSchema(BaseModel):
    """Complete inspected schema representation for a MongoDB database."""

    database_name: str = Field(..., description="Name of the inspected database")
    collections: dict[str, MongoCollectionSchema] = Field(
        default_factory=dict, description="Collection schemas keyed by collection name"
    )
    sample_limit: int = Field(default=50, description="Configured sample document limit per collection")
    accessible_databases: list[str] = Field(
        default_factory=list, description="List of accessible database names discovered on the cluster"
    )
    server_version: Optional[str] = Field(default=None, description="MongoDB server version")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Additional cluster and connection metadata"
    )
