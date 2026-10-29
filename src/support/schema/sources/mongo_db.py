"""Live MongoDB schema inspection source using bounded document sampling."""

from typing import Any, Optional
from support.core.dialect import Dialect
from support.core.exceptions import DatabaseAccessError
from support.db.health import verify_driver_available
from support.db.models import ConnectionProfile
from support.schema.mongo_models import (
    MongoDatabaseSchema,
    MongoCollectionSchema,
    MongoFieldSchema,
    MongoIndexSchema,
)


def _get_bson_type_name(val: Any) -> str:
    """Map Python / BSON value to a friendly MongoDB type name."""
    if val is None:
        return "null"
    type_name = type(val).__name__
    if type_name == "ObjectId":
        return "objectId"
    if type_name == "datetime":
        return "date"
    if isinstance(val, bool):
        return "bool"
    if isinstance(val, int):
        return "int"
    if isinstance(val, float):
        return "double"
    if isinstance(val, str):
        return "string"
    if isinstance(val, dict):
        return "object"
    if isinstance(val, list):
        return "array"
    if isinstance(val, bytes):
        return "binData"
    return type_name.lower()


class MongoDatabaseSchemaSource:
    """Discovers schema from a live MongoDB database via bounded document sampling.

    Unlike relational databases with enforced SQL DDL schemas, MongoDB collections
    contain flexible documents. This inspector bounds memory and I/O by examining
    at most `sample_limit` documents per collection and reports observed BSON types,
    heterogeneity, and presence frequency.
    """

    DEFAULT_SAMPLE_LIMIT = 50

    def __init__(
        self,
        profile: ConnectionProfile,
        password: Optional[str] = None,
        sample_limit: int = DEFAULT_SAMPLE_LIMIT,
        timeout: float = 5.0,
    ) -> None:
        self.profile = profile
        self.password = password
        self.sample_limit = max(1, sample_limit)
        self.timeout = timeout

    def discover(self) -> MongoDatabaseSchema:
        """Inspect the live MongoDB database and return discovered collection schemas."""
        verify_driver_available(Dialect.MONGODB)

        try:
            import pymongo
            from pymongo.errors import (
                ConfigurationError,
                ConnectionFailure,
                OperationFailure,
                ServerSelectionTimeoutError,
            )
        except ImportError as e:
            raise DatabaseAccessError(
                f"PyMongo is required for MongoDB inspection. Install with 'pip install pymongo'."
            ) from e

        target_db_name = self.profile.database or "test"
        client = None

        try:
            connect_kwargs: dict[str, Any] = {
                "serverSelectionTimeoutMS": int(self.timeout * 1000),
                "connectTimeoutMS": int(self.timeout * 1000),
                "socketTimeoutMS": int(self.timeout * 1000),
            }

            user = self.profile.username
            client_kwargs: dict[str, Any] = dict(connect_kwargs)
            if user and self.password:
                client_kwargs["username"] = user
                client_kwargs["password"] = self.password
            auth_source = self.profile.options.get("authSource")
            if auth_source:
                client_kwargs["authSource"] = auth_source

            if self.profile.options.get("srv"):
                uri = f"mongodb+srv://{self.profile.host}"
                client = pymongo.MongoClient(uri, **client_kwargs)
            else:
                host = self.profile.host or "localhost"
                port = self.profile.port or 27017
                client = pymongo.MongoClient(host=host, port=port, **client_kwargs)


            # Test connection & retrieve server version
            server_info = client.admin.command("ping")
            server_version: Optional[str] = None
            try:
                build_info = client.admin.command("buildInfo")
                server_version = build_info.get("version")
            except Exception:
                pass

            # Discover accessible databases
            accessible_dbs: list[str] = []
            try:
                accessible_dbs = client.list_database_names()
            except Exception:
                accessible_dbs = [target_db_name]

            db = client[target_db_name]

            # List collections
            try:
                collection_names = db.list_collection_names()
            except OperationFailure as e:
                raise DatabaseAccessError(
                    f"Permission denied accessing collections in database '{target_db_name}': {e.details.get('errmsg', str(e))}"
                ) from e

            collections: dict[str, MongoCollectionSchema] = {}

            for col_name in collection_names:
                if col_name.startswith("system."):
                    continue

                col = db[col_name]
                col_schema = self._inspect_collection(col, col_name)
                collections[col_name] = col_schema

            return MongoDatabaseSchema(
                database_name=target_db_name,
                collections=collections,
                sample_limit=self.sample_limit,
                accessible_databases=accessible_dbs,
                server_version=server_version,
                metadata={
                    "profile_name": self.profile.name,
                    "target": self.profile.display_target,
                    "redacted_url": self.profile.get_redacted_url(),
                },
            )

        except (ServerSelectionTimeoutError, ConnectionFailure) as e:
            raise DatabaseAccessError(
                self.profile.display_target,
                f"Could not connect to MongoDB cluster at {self.profile.display_target} (timed out after {self.timeout}s).",
            ) from e
        except OperationFailure as e:
            err_msg = e.details.get("errmsg", str(e)) if hasattr(e, "details") and e.details else str(e)
            raise DatabaseAccessError(self.profile.display_target, f"MongoDB operation failed: {err_msg}") from e
        except ConfigurationError as e:
            raise DatabaseAccessError(self.profile.display_target, f"Invalid MongoDB configuration: {e}") from e
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def _inspect_collection(self, col: Any, col_name: str) -> MongoCollectionSchema:
        """Inspect a single collection using bounded sampling."""
        # 1. Estimate count
        is_empty = False
        est_count: Optional[int] = None
        try:
            est_count = col.estimated_document_count()
            if est_count == 0:
                is_empty = True
        except Exception:
            pass

        # 2. Inspect Indexes
        indexes: list[MongoIndexSchema] = []
        try:
            raw_indexes = col.list_indexes()
            for idx in raw_indexes:
                idx_name = idx.get("name", "")
                idx_keys = idx.get("key", {}).items() if isinstance(idx.get("key"), dict) else []
                indexes.append(
                    MongoIndexSchema(
                        name=idx_name,
                        keys=list(idx_keys),
                        unique=idx.get("unique", False),
                    )
                )
        except Exception:
            pass

        if is_empty:
            return MongoCollectionSchema(
                name=col_name,
                estimated_count=est_count,
                sample_size=0,
                is_empty=True,
                fields=[],
                indexes=indexes,
            )

        # 3. Sample documents
        field_counts: dict[str, int] = {}
        field_types: dict[str, set[str]] = {}
        field_nested: dict[str, bool] = {}

        sample_docs: list[dict[str, Any]] = []
        try:
            cursor = col.find().limit(self.sample_limit)
            sample_docs = list(cursor)
        except Exception:
            sample_docs = []

        sample_size = len(sample_docs)
        if sample_size == 0:
            return MongoCollectionSchema(
                name=col_name,
                estimated_count=est_count,
                sample_size=0,
                is_empty=True,
                fields=[],
                indexes=indexes,
            )

        def extract_fields(doc: dict[str, Any], prefix: str = "", depth: int = 1) -> None:
            if depth > 3:
                return
            for key, val in doc.items():
                field_path = f"{prefix}.{key}" if prefix else str(key)
                field_counts[field_path] = field_counts.get(field_path, 0) + 1

                t_name = _get_bson_type_name(val)
                if field_path not in field_types:
                    field_types[field_path] = set()
                field_types[field_path].add(t_name)

                if isinstance(val, dict):
                    field_nested[field_path] = True
                    extract_fields(val, prefix=field_path, depth=depth + 1)
                elif isinstance(val, list):
                    field_nested[field_path] = True
                    # Check list element types
                    for item in val[:5]:  # sample up to 5 items in array
                        item_t = _get_bson_type_name(item)
                        field_types[field_path].add(f"array[{item_t}]")
                        if isinstance(item, dict):
                            extract_fields(item, prefix=f"{field_path}[]", depth=depth + 1)

        for doc in sample_docs:
            if isinstance(doc, dict):
                extract_fields(doc)

        # Build field schemas sorted alphabetically with _id first
        sorted_field_names = sorted(
            field_counts.keys(),
            key=lambda k: (0 if k == "_id" else 1, k),
        )

        fields: list[MongoFieldSchema] = []
        for name in sorted_field_names:
            observed_types = sorted(field_types.get(name, set()))
            count = field_counts.get(name, 0)
            nullable = (count < sample_size) or ("null" in observed_types)
            fields.append(
                MongoFieldSchema(
                    name=name,
                    types=observed_types,
                    count=count,
                    sample_size=sample_size,
                    nullable=nullable,
                    has_nested=field_nested.get(name, False),
                )
            )

        return MongoCollectionSchema(
            name=col_name,
            estimated_count=est_count,
            sample_size=sample_size,
            is_empty=False,
            fields=fields,
            indexes=indexes,
        )
