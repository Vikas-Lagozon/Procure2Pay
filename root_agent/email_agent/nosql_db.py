# nosql_db.py
# ─────────────────────────────────────────────────────────────
# Generalised MongoDB Module
#
# Architecture — two layers:
#
#   MongoDBConnection  →  owns the single MongoClient (singleton)
#   MongoCollection    →  lightweight proxy for ONE collection;
#                         instantiate once per collection name in
#                         any module that needs MongoDB access.
#
# ─────────────────────────────────────────────────────────────

from __future__ import annotations

from typing import Any

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import ConnectionFailure, OperationFailure, PyMongoError

from config import config
from logger import get_logger

logger = get_logger(__name__)

# Type alias for sort tuples used across the API
SortOrder = tuple[str, int]


# ─────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────

def _stringify_ids(doc: dict) -> dict:
    """Convert ObjectId _id to str in-place and return the doc."""
    if doc and "_id" in doc:
        doc["_id"] = str(doc["_id"])
    return doc


def _to_object_id(value: str) -> ObjectId:
    """
    Convert a hex string to ObjectId.
    Raises ValueError with a friendly message on bad input.
    """
    try:
        return ObjectId(value)
    except (InvalidId, TypeError) as exc:
        raise ValueError(f"Invalid ObjectId string: '{value}'") from exc


_MONGO_OPERATORS = frozenset({
    "$set", "$unset", "$push", "$pull", "$addToSet",
    "$inc", "$mul", "$rename", "$min", "$max", "$currentDate",
    "$pop", "$pullAll", "$bit",
})


def _wrap_set(update: dict[str, Any]) -> dict[str, Any]:
    """
    If *update* contains no top-level operator key, wrap it in ``$set``
    so callers can pass plain field dicts:

        update_one(filter, {"status": "approved"})
        # becomes → {"$set": {"status": "approved"}}
    """
    if not any(k in _MONGO_OPERATORS for k in update):
        return {"$set": update}
    return update


# ─────────────────────────────────────────────────────────────
# Layer 1 — MongoDBConnection  (internal singleton)
# ─────────────────────────────────────────────────────────────

class MongoDBConnection:
    """
    Manages a single MongoClient for the whole application.

    Auth is already handled inside config.py — if MONGO_USER /
    MONGO_PASSWORD are set, config.MONGO_URI already contains the
    credentials. This class never touches auth logic.
    """

    def __init__(self) -> None:
        self._client: MongoClient | None = None
        self._db: Database | None = None

    def connect(self) -> None:
        """
        Open the MongoClient and verify the connection with a ping.
        Safe to call multiple times — subsequent calls are no-ops.
        """
        if self._client is not None:
            logger.debug("MongoDBConnection already active — skipping reconnect.")
            return

        try:
            logger.info(f"Connecting to MongoDB | db={config.MONGO_DB}")
            self._client = MongoClient(
                config.MONGO_URI,
                serverSelectionTimeoutMS=5_000,
                connectTimeoutMS=5_000,
            )
            self._client.admin.command("ping")      # raises on failure
            self._db = self._client[config.MONGO_DB]
            logger.info("MongoDB connection established.")

        except ConnectionFailure as exc:
            logger.error(f"MongoDB connection failed: {exc}")
            raise RuntimeError(f"Could not connect to MongoDB: {exc}") from exc
        except OperationFailure as exc:
            logger.error(f"MongoDB auth/operation error: {exc}")
            raise RuntimeError(f"MongoDB auth/operation error: {exc}") from exc

    def _ensure_connected(self) -> None:
        if self._client is None or self._db is None:
            raise RuntimeError(
                "MongoDB client is not initialised. Call connect() first."
            )

    def get_collection(self, name: str) -> Collection:
        """Return a raw PyMongo Collection object by name."""
        self._ensure_connected()
        return self._db[name]   # type: ignore[index]

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None
            self._db = None
            logger.info("MongoDB connection closed.")

    def __enter__(self) -> "MongoDBConnection":
        self.connect()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


# ── Module-level singleton ────────────────────────────────────

_connection: MongoDBConnection | None = None


def _get_connection() -> MongoDBConnection:
    """Return (and lazily connect) the shared MongoDBConnection."""
    global _connection
    if _connection is None:
        _connection = MongoDBConnection()
        _connection.connect()
    return _connection


# ─────────────────────────────────────────────────────────────
# Layer 2 — MongoCollection  (public, generalised)
# ─────────────────────────────────────────────────────────────

class MongoCollection:
    """
    Collection-scoped CRUD proxy.  Instantiate once per collection:

        requirements = MongoCollection("requirements")
        vendors      = MongoCollection("vendors")

    Every method operates exclusively on that collection.
    All ObjectIds are automatically converted to str so callers
    never need to import bson.

    Methods
    -------
    CREATE
        insert_one(document)            → str (_id)
        insert_many(documents)          → list[str] (_ids)

    READ
        fetch_one(filter, projection)   → dict | None
        fetch_by_id(id)                 → dict | None
        fetch_all(filter, projection,   → list[dict]
                  sort, limit, skip)

    UPDATE
        update_one(filter, update,      → int (matched_count)
                   upsert)
        update_by_id(id, update,        → int
                     upsert)
        update_many(filter, update,     → int (matched_count)
                    upsert)

    DELETE
        delete_one(filter)              → int (deleted_count)
        delete_by_id(id)                → int
        delete_many(filter)             → int (deleted_count)

    UTILITY
        count(filter)                   → int
        exists(filter)                  → bool
        aggregate(pipeline)             → list[dict]
        create_index(keys, unique)      → str (index name)
    """

    def __init__(self, collection_name: str) -> None:
        if not collection_name or not isinstance(collection_name, str):
            raise ValueError("collection_name must be a non-empty string.")
        self._name = collection_name
        logger.debug(f"MongoCollection initialised | collection='{self._name}'")

    @property
    def _col(self) -> Collection:
        return _get_connection().get_collection(self._name)

    # ─────────────────────────────────────────────────────────
    # CREATE
    # ─────────────────────────────────────────────────────────

    def insert_one(self, document: dict[str, Any]) -> str:
        """
        Insert a single document into the collection.

        Parameters
        ----------
        document : dict
            The document to insert.

        Returns
        -------
        str
            The inserted document's ``_id`` as a string.
        """
        try:
            logger.info(f"[{self._name}] insert_one")
            result = self._col.insert_one(document)
            _id = str(result.inserted_id)
            logger.info(f"[{self._name}] Inserted | _id={_id}")
            return _id
        except PyMongoError as exc:
            logger.error(f"[{self._name}] insert_one failed: {exc}")
            raise RuntimeError(f"insert_one failed in '{self._name}': {exc}") from exc

    def insert_many(self, documents: list[dict[str, Any]]) -> list[str]:
        """
        Insert multiple documents in a single round-trip.

        Parameters
        ----------
        documents : list[dict]
            Documents to insert. Empty list is a no-op.

        Returns
        -------
        list[str]
            Inserted ``_id`` values as strings, in insertion order.
        """
        if not documents:
            logger.warning(f"[{self._name}] insert_many called with empty list — skipped.")
            return []
        try:
            logger.info(f"[{self._name}] insert_many | count={len(documents)}")
            result = self._col.insert_many(documents)
            ids = [str(oid) for oid in result.inserted_ids]
            logger.info(f"[{self._name}] Inserted {len(ids)} documents.")
            return ids
        except PyMongoError as exc:
            logger.error(f"[{self._name}] insert_many failed: {exc}")
            raise RuntimeError(f"insert_many failed in '{self._name}': {exc}") from exc

    # ─────────────────────────────────────────────────────────
    # READ
    # ─────────────────────────────────────────────────────────

    def fetch_one(
        self,
        filter: dict[str, Any],
        projection: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """
        Fetch the first document matching *filter*.

        Parameters
        ----------
        filter : dict
            PyMongo query filter, e.g. ``{"vendor_code": "V-001"}``.
            To look up by ``_id`` string, use ``fetch_by_id()``.
        projection : dict, optional
            Fields to include/exclude, e.g. ``{"password": 0}``.

        Returns
        -------
        dict | None
            Document with ObjectId converted to str, or ``None``.
        """
        try:
            logger.debug(f"[{self._name}] fetch_one | filter={filter}")
            doc = self._col.find_one(filter, projection)
            return _stringify_ids(doc) if doc else None
        except PyMongoError as exc:
            logger.error(f"[{self._name}] fetch_one failed: {exc}")
            raise RuntimeError(f"fetch_one failed in '{self._name}': {exc}") from exc

    def fetch_by_id(self, id: str) -> dict[str, Any] | None:
        """
        Fetch a single document by its ``_id`` string.

        Parameters
        ----------
        id : str
            Hex string of the MongoDB ObjectId.

        Returns
        -------
        dict | None
        """
        return self.fetch_one({"_id": _to_object_id(id)})

    def fetch_all(
        self,
        filter: dict[str, Any] | None = None,
        projection: dict[str, Any] | None = None,
        sort: list[SortOrder] | None = None,
        limit: int = 0,
        skip: int = 0,
    ) -> list[dict[str, Any]]:
        """
        Fetch all documents matching *filter*.

        Parameters
        ----------
        filter : dict, optional
            Query filter. ``None`` / ``{}`` returns every document.
        projection : dict, optional
            Fields to include/exclude.
        sort : list of (field, direction) tuples, optional
            e.g. ``[("upload_timestamp", -1), ("file_name", 1)]``.
            Use ``1`` (ascending) or ``-1`` (descending).
        limit : int
            Max documents to return (0 = no limit).
        skip : int
            Documents to skip — useful for pagination.

        Returns
        -------
        list[dict]
            Documents with ObjectIds converted to str.
        """
        query = filter or {}
        try:
            logger.debug(
                f"[{self._name}] fetch_all | filter={query} "
                f"sort={sort} limit={limit} skip={skip}"
            )
            cursor = self._col.find(query, projection)
            if sort:
                cursor = cursor.sort(sort)
            if skip:
                cursor = cursor.skip(skip)
            if limit:
                cursor = cursor.limit(limit)

            docs = [_stringify_ids(doc) for doc in cursor]
            logger.info(f"[{self._name}] fetch_all → {len(docs)} document(s).")
            return docs
        except PyMongoError as exc:
            logger.error(f"[{self._name}] fetch_all failed: {exc}")
            raise RuntimeError(f"fetch_all failed in '{self._name}': {exc}") from exc

    # ─────────────────────────────────────────────────────────
    # UPDATE
    # ─────────────────────────────────────────────────────────

    def update_one(
        self,
        filter: dict[str, Any],
        update: dict[str, Any],
        upsert: bool = False,
    ) -> int:
        """
        Update the first document matching *filter*.

        Parameters
        ----------
        filter : dict
            Query filter to locate the document.
        update : dict
            Update payload.  Plain field dicts are automatically
            wrapped in ``$set``:
                ``{"status": "approved"}``
                → ``{"$set": {"status": "approved"}}``
            Pass explicit operators (``$push``, ``$inc``, etc.) to
            bypass the auto-wrap.
        upsert : bool
            Create document if no match is found.

        Returns
        -------
        int
            ``matched_count`` (0 or 1).
        """
        try:
            logger.info(f"[{self._name}] update_one | filter={filter} upsert={upsert}")
            result = self._col.update_one(filter, _wrap_set(update), upsert=upsert)
            logger.info(
                f"[{self._name}] update_one | matched={result.matched_count} "
                f"modified={result.modified_count} upserted={result.upserted_id}"
            )
            return result.matched_count
        except PyMongoError as exc:
            logger.error(f"[{self._name}] update_one failed: {exc}")
            raise RuntimeError(f"update_one failed in '{self._name}': {exc}") from exc

    def update_by_id(
        self,
        id: str,
        update: dict[str, Any],
        upsert: bool = False,
    ) -> int:
        """
        Update a single document located by its ``_id`` string.

        Returns
        -------
        int
            ``matched_count``.
        """
        return self.update_one({"_id": _to_object_id(id)}, update, upsert=upsert)

    def update_many(
        self,
        filter: dict[str, Any],
        update: dict[str, Any],
        upsert: bool = False,
    ) -> int:
        """
        Update ALL documents matching *filter*.

        Returns
        -------
        int
            ``matched_count``.
        """
        try:
            logger.info(f"[{self._name}] update_many | filter={filter} upsert={upsert}")
            result = self._col.update_many(filter, _wrap_set(update), upsert=upsert)
            logger.info(
                f"[{self._name}] update_many | matched={result.matched_count} "
                f"modified={result.modified_count}"
            )
            return result.matched_count
        except PyMongoError as exc:
            logger.error(f"[{self._name}] update_many failed: {exc}")
            raise RuntimeError(f"update_many failed in '{self._name}': {exc}") from exc

    # ─────────────────────────────────────────────────────────
    # DELETE
    # ─────────────────────────────────────────────────────────

    def delete_one(self, filter: dict[str, Any]) -> int:
        """
        Delete the first document matching *filter*.

        Returns
        -------
        int
            ``deleted_count`` (0 or 1).
        """
        try:
            logger.info(f"[{self._name}] delete_one | filter={filter}")
            result = self._col.delete_one(filter)
            logger.info(f"[{self._name}] delete_one | deleted={result.deleted_count}")
            return result.deleted_count
        except PyMongoError as exc:
            logger.error(f"[{self._name}] delete_one failed: {exc}")
            raise RuntimeError(f"delete_one failed in '{self._name}': {exc}") from exc

    def delete_by_id(self, id: str) -> int:
        """Delete a single document located by its ``_id`` string."""
        return self.delete_one({"_id": _to_object_id(id)})

    def delete_many(self, filter: dict[str, Any]) -> int:
        """
        Delete ALL documents matching *filter*.

        Returns
        -------
        int
            ``deleted_count``.
        """
        try:
            logger.info(f"[{self._name}] delete_many | filter={filter}")
            result = self._col.delete_many(filter)
            logger.info(f"[{self._name}] delete_many | deleted={result.deleted_count}")
            return result.deleted_count
        except PyMongoError as exc:
            logger.error(f"[{self._name}] delete_many failed: {exc}")
            raise RuntimeError(f"delete_many failed in '{self._name}': {exc}") from exc

    # ─────────────────────────────────────────────────────────
    # UTILITY
    # ─────────────────────────────────────────────────────────

    def count(self, filter: dict[str, Any] | None = None) -> int:
        """
        Count documents matching *filter*.
        ``None`` / ``{}`` counts the entire collection.

        Returns
        -------
        int
        """
        query = filter or {}
        try:
            n = self._col.count_documents(query)
            logger.debug(f"[{self._name}] count | filter={query} → {n}")
            return n
        except PyMongoError as exc:
            logger.error(f"[{self._name}] count failed: {exc}")
            raise RuntimeError(f"count failed in '{self._name}': {exc}") from exc

    def exists(self, filter: dict[str, Any]) -> bool:
        """
        Return ``True`` if at least one document matches *filter*.
        More efficient than ``fetch_one`` — only retrieves ``_id``.

        Returns
        -------
        bool
        """
        try:
            result = self._col.find_one(filter, {"_id": 1})
            return result is not None
        except PyMongoError as exc:
            logger.error(f"[{self._name}] exists check failed: {exc}")
            raise RuntimeError(f"exists failed in '{self._name}': {exc}") from exc

    def aggregate(self, pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Run an aggregation pipeline and return all result documents.

        Parameters
        ----------
        pipeline : list[dict]
            Standard MongoDB aggregation stages,
            e.g. ``[{"$match": {...}}, {"$group": {...}}]``.

        Returns
        -------
        list[dict]
            Result documents with ObjectIds converted to str.
        """
        try:
            logger.info(f"[{self._name}] aggregate | stages={len(pipeline)}")
            docs = [_stringify_ids(doc) for doc in self._col.aggregate(pipeline)]
            logger.info(f"[{self._name}] aggregate → {len(docs)} result(s).")
            return docs
        except PyMongoError as exc:
            logger.error(f"[{self._name}] aggregate failed: {exc}")
            raise RuntimeError(f"aggregate failed in '{self._name}': {exc}") from exc

    def create_index(
        self,
        keys: list[SortOrder],
        unique: bool = False,
        **kwargs: Any,
    ) -> str:
        """
        Create an index on the collection.

        Parameters
        ----------
        keys : list of (field, direction) tuples
            e.g. ``[("email", 1)]``.
        unique : bool
            Enforce uniqueness constraint.

        Returns
        -------
        str
            The name of the created index.
        """
        try:
            name = self._col.create_index(keys, unique=unique, **kwargs)
            logger.info(f"[{self._name}] Index created | name={name} unique={unique}")
            return name
        except PyMongoError as exc:
            logger.error(f"[{self._name}] create_index failed: {exc}")
            raise RuntimeError(f"create_index failed in '{self._name}': {exc}") from exc

    def __repr__(self) -> str:
        return f"MongoCollection(collection='{self._name}', db='{config.MONGO_DB}')"

