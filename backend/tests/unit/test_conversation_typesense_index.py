"""First-party conversation Typesense projection — payload, gating, fail-open.

Covers the contract from SCA-452:
- upsert payload shape matches the `conversations` collection schema the
  Firebase extension created and `utils.conversations.search` queries;
- ``transcript_segments`` / speaker fields can never reach Typesense;
- e2ee accounts are skipped by deleting any previously indexed document;
- Typesense being down never raises out of the conversation write paths;
- account-deletion purge is filter-based and fail-closed when required.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault(
    "ENCRYPTION_SECRET",
    "omi_ZwB2ZNqB2HHpMK6wStk7sTpavJiPTFg7gXUHnc4tFABPU6pZ2c2DKgehtfgi4RZv",
)
os.environ.setdefault("TYPESENSE_HOST", "localhost")
os.environ.setdefault("TYPESENSE_HOST_PORT", "8108")
os.environ.setdefault("TYPESENSE_API_KEY", "test-key-not-real")

from utils.conversations import typesense_index
from utils.conversations.typesense_index import (
    build_conversation_index_document,
    conversation_index_writes_enabled,
    delete_conversation_index_doc,
    purge_user_conversation_index,
    sync_conversation_index_after_write,
)
from database import conversations as conversations_db

UID = "uid-conv-typesense"
CONVERSATION_ID = "conv-1"

ALLOWED_FIELDS = frozenset(
    {"id", "userId", "created_at", "discarded", "started_at", "finished_at", "structured", "geolocation"}
)


def _conversation_data() -> dict:
    return {
        "id": CONVERSATION_ID,
        "created_at": datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc),
        "started_at": datetime(2026, 9, 5, 11, 58, 30, tzinfo=timezone.utc),
        "finished_at": datetime(2026, 9, 5, 12, 4, 0, tzinfo=timezone.utc),
        "discarded": False,
        "structured": {"title": "Standup", "overview": "Daily sync", "events": [], "action_items": []},
        "geolocation": {"latitude": 37.7749, "longitude": -122.4194, "google_place_id": "place-x"},
        # Fields that must never reach Typesense.
        "transcript_segments": [
            {"text": "hello", "is_user": True, "person_id": "speaker-1"},
            {"text": "world", "is_user": False, "person_id": "speaker-2"},
        ],
        "photos": [{"base64": "zzz"}],
        "apps_results": [{"app_id": "a1", "content": "app output"}],
    }


def _fake_typesense() -> tuple[MagicMock, dict]:
    docs_store: dict = {}
    typesense_client = MagicMock()

    def _upsert(doc):
        docs_store[doc["id"]] = doc
        return doc

    def _delete_filter(params):
        filter_by = params.get("filter_by", "")
        quoted = [value for value in filter_by.split("`") if value and ":" not in value and "[" not in value]
        user_id = quoted[0] if quoted else None
        to_delete = [doc_id for doc_id, doc in docs_store.items() if doc.get("userId") == user_id]
        for doc_id in to_delete:
            docs_store.pop(doc_id, None)
        return {"num_deleted": len(to_delete)}

    documents = MagicMock()
    documents.upsert.side_effect = _upsert
    documents.delete.side_effect = _delete_filter
    documents.__getitem__.side_effect = lambda doc_id: MagicMock(delete=lambda: docs_store.pop(doc_id, None))

    collection = MagicMock()
    collection.documents = documents
    typesense_client.collections.__getitem__.return_value = collection
    return typesense_client, docs_store


def _policy_db(level: str = "enhanced") -> MagicMock:
    user_doc = MagicMock(exists=True, to_dict=lambda: {"data_protection_level": level})
    db_client = MagicMock()
    db_client.document.return_value = MagicMock(get=lambda: user_doc)
    return db_client


def _firestore_with_doc(doc: dict | None) -> MagicMock:
    snapshot = MagicMock()
    snapshot.exists = doc is not None
    snapshot.to_dict = lambda: dict(doc) if doc is not None else None
    client = MagicMock()
    client.collection.return_value.document.return_value.collection.return_value.document.return_value.get.return_value = (
        snapshot
    )
    return client


@pytest.fixture
def index_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TYPESENSE_HOST", "localhost")
    monkeypatch.setenv("TYPESENSE_API_KEY", "test-key-not-real")
    monkeypatch.delenv(typesense_index.CONVERSATION_INDEX_WRITES_ENV, raising=False)


@pytest.fixture
def mock_typesense():
    typesense_client, docs_store = _fake_typesense()
    with (
        patch.object(typesense_index, "_typesense_client", return_value=typesense_client),
        patch.object(typesense_index, "default_db_client", _policy_db()),
    ):
        yield typesense_client, docs_store


class TestDocumentShape:
    def test_payload_carries_exactly_the_schema_fields(self):
        document = build_conversation_index_document(UID, _conversation_data())

        assert document is not None
        assert set(document) == ALLOWED_FIELDS
        assert document["id"] == CONVERSATION_ID
        assert document["userId"] == UID
        assert document["created_at"] == 1788609600  # 2026-09-05T12:00:00Z in unix seconds
        assert document["started_at"] == 1788609510
        assert document["finished_at"] == 1788609840
        assert document["discarded"] is False
        assert document["structured"]["title"] == "Standup"
        assert document["geolocation"] == [37.7749, -122.4194]

    def test_transcript_segments_and_speaker_fields_never_leave(self):
        document = build_conversation_index_document(UID, _conversation_data())

        dumped = str(document)
        assert "transcript_segments" not in dumped
        assert "speaker-1" not in dumped
        assert "speaker-2" not in dumped
        assert "is_user" not in dumped
        assert "zzz" not in dumped

    def test_epoch_numbers_pass_through(self):
        document = build_conversation_index_document(
            UID,
            {
                "id": "c2",
                "created_at": 1788609600,
                "started_at": 1788609510.25,
                "discarded": True,
            },
        )

        assert document is not None
        assert document["created_at"] == 1788609600
        assert document["started_at"] == 1788609510
        assert document["discarded"] is True

    def test_naive_datetime_is_treated_as_utc(self):
        document = build_conversation_index_document(UID, {"id": "c3", "created_at": datetime(2026, 9, 5, 12, 0, 0)})

        assert document is not None
        assert document["created_at"] == 1788609600

    def test_unparseable_geolocation_is_omitted_not_fatal(self):
        document = build_conversation_index_document(
            UID,
            {
                "id": "c4",
                "created_at": 1788609600,
                "geolocation": {"latitude": "north", "longitude": None},
            },
        )

        assert document is not None
        assert "geolocation" not in document

    def test_geopoint_object_maps_to_typesense_point(self):
        geopoint = MagicMock(latitude=52.52, longitude=13.405)

        document = build_conversation_index_document(
            UID, {"id": "c5", "created_at": 1788609600, "geolocation": geopoint}
        )

        assert document is not None
        assert document["geolocation"] == [52.52, 13.405]

    def test_missing_created_at_is_unindexable(self):
        assert build_conversation_index_document(UID, {"id": "c6"}) is None
        assert build_conversation_index_document(UID, {"id": "", "created_at": 1788609600}) is None


class TestWriteFlag:
    def test_default_on_when_typesense_env_present(self, index_env, monkeypatch):
        assert conversation_index_writes_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "off", "no", "OFF"])
    def test_kill_switch_disables(self, index_env, monkeypatch, value):
        monkeypatch.setenv(typesense_index.CONVERSATION_INDEX_WRITES_ENV, value)
        assert conversation_index_writes_enabled() is False

    def test_missing_typesense_env_disables(self, monkeypatch):
        monkeypatch.delenv("TYPESENSE_HOST", raising=False)
        monkeypatch.delenv("TYPESENSE_API_KEY", raising=False)
        assert conversation_index_writes_enabled() is False


class TestSync:
    def test_sync_upserts_projected_document(self, index_env, mock_typesense):
        typesense_client, docs_store = mock_typesense
        firestore = _firestore_with_doc(_conversation_data())

        assert sync_conversation_index_after_write(UID, CONVERSATION_ID, firestore_client=firestore) is True

        assert set(docs_store) == {CONVERSATION_ID}
        assert set(docs_store[CONVERSATION_ID]) == ALLOWED_FIELDS
        typesense_client.collections.__getitem__.assert_called_with("conversations")

    def test_sync_skips_when_disabled(self, index_env, monkeypatch, mock_typesense):
        typesense_client, _ = mock_typesense
        monkeypatch.setenv(typesense_index.CONVERSATION_INDEX_WRITES_ENV, "0")
        firestore = _firestore_with_doc(_conversation_data())

        assert sync_conversation_index_after_write(UID, CONVERSATION_ID, firestore_client=firestore) is False

        typesense_client.collections.__getitem__.return_value.documents.upsert.assert_not_called()

    def test_e2ee_user_deletes_instead_of_upserting(self, index_env, mock_typesense):
        typesense_client, docs_store = mock_typesense
        docs_store[CONVERSATION_ID] = {"id": CONVERSATION_ID, "userId": UID}
        firestore = _firestore_with_doc(_conversation_data())

        with patch.object(typesense_index, "default_db_client", _policy_db(level="e2ee")):
            assert sync_conversation_index_after_write(UID, CONVERSATION_ID, firestore_client=firestore) is True

        assert docs_store == {}
        typesense_client.collections.__getitem__.return_value.documents.upsert.assert_not_called()

    def test_missing_firestore_doc_converges_to_delete(self, index_env, mock_typesense):
        _, docs_store = mock_typesense
        docs_store[CONVERSATION_ID] = {"id": CONVERSATION_ID, "userId": UID}
        firestore = _firestore_with_doc(None)

        assert sync_conversation_index_after_write(UID, CONVERSATION_ID, firestore_client=firestore) is True

        assert docs_store == {}

    def test_typesense_down_does_not_raise(self, index_env, mock_typesense):
        typesense_client, _ = mock_typesense
        typesense_client.collections.__getitem__.return_value.documents.upsert.side_effect = Exception(
            "connection refused"
        )
        firestore = _firestore_with_doc(_conversation_data())

        assert sync_conversation_index_after_write(UID, CONVERSATION_ID, firestore_client=firestore) is False

    def test_projection_read_failure_does_not_raise(self, index_env, mock_typesense):
        firestore = _firestore_with_doc(_conversation_data())
        firestore.collection.return_value.document.return_value.collection.return_value.document.return_value.get.side_effect = Exception(
            "firestore unavailable"
        )

        assert sync_conversation_index_after_write(UID, CONVERSATION_ID, firestore_client=firestore) is False

    def test_internal_never_raise_contract(self, index_env, mock_typesense):
        firestore = _firestore_with_doc(_conversation_data())

        with patch.object(
            typesense_index,
            "_sync_conversation_index_after_write",
            side_effect=Exception("unexpected bug"),
        ):
            assert sync_conversation_index_after_write(UID, CONVERSATION_ID, firestore_client=firestore) is False


class TestDelete:
    def test_delete_removes_by_conversation_id(self, mock_typesense):
        _, docs_store = mock_typesense
        docs_store[CONVERSATION_ID] = {"id": CONVERSATION_ID, "userId": UID}

        assert delete_conversation_index_doc(UID, CONVERSATION_ID) is True
        assert docs_store == {}

    def test_object_not_found_is_success(self, mock_typesense):
        typesense_client, _ = mock_typesense

        class ObjectNotFound(Exception):
            pass

        ObjectNotFound.__module__ = "typesense.exceptions"
        failing_doc = MagicMock()
        failing_doc.delete.side_effect = ObjectNotFound("not found")
        typesense_client.collections.__getitem__.return_value.documents.__getitem__.side_effect = (
            lambda doc_id: failing_doc
        )

        assert delete_conversation_index_doc(UID, "missing-conv") is True

    def test_typesense_down_does_not_raise_on_delete(self, mock_typesense):
        typesense_client, _ = mock_typesense
        failing_doc = MagicMock()
        failing_doc.delete.side_effect = Exception("timeout")
        typesense_client.collections.__getitem__.return_value.documents.__getitem__.side_effect = (
            lambda doc_id: failing_doc
        )

        assert delete_conversation_index_doc(UID, CONVERSATION_ID) is False

    def test_unconfigured_typesense_is_a_silent_noop(self, monkeypatch):
        monkeypatch.delenv("TYPESENSE_HOST", raising=False)
        monkeypatch.delenv("TYPESENSE_API_KEY", raising=False)

        with patch.object(typesense_index, "_typesense_client", side_effect=AssertionError("client constructed")):
            assert delete_conversation_index_doc(UID, CONVERSATION_ID) is False


class TestPurge:
    def test_purge_deletes_all_user_documents(self, mock_typesense):
        _, docs_store = mock_typesense
        docs_store.update(
            {
                "c1": {"id": "c1", "userId": UID},
                "c2": {"id": "c2", "userId": UID},
                "other": {"id": "other", "userId": "someone-else"},
            }
        )

        assert purge_user_conversation_index(UID) == 2
        assert set(docs_store) == {"other"}

    def test_purge_raises_when_required(self, mock_typesense):
        typesense_client, _ = mock_typesense
        typesense_client.collections.__getitem__.return_value.documents.delete.side_effect = Exception("typesense down")

        with pytest.raises(Exception, match="typesense down"):
            purge_user_conversation_index(UID, raise_on_failure=True)

    def test_purge_is_silent_noop_when_unconfigured(self, monkeypatch):
        monkeypatch.delenv("TYPESENSE_HOST", raising=False)
        monkeypatch.delenv("TYPESENSE_API_KEY", raising=False)

        assert purge_user_conversation_index(UID) == 0


class TestConversationWriteWiring:
    """The durable write paths must converge the index and must never leak a
    Typesense failure into the caller (SCA-452 fail-open requirement)."""

    @pytest.fixture
    def conversations_db(self, monkeypatch: pytest.MonkeyPatch):
        # conversations_db is imported at module scope on purpose: binding the
        # real module object at collection time keeps this suite independent of
        # import-order games other test modules play with sys.modules.
        index = MagicMock()
        monkeypatch.setattr(conversations_db, "conversation_typesense_index", index)

        conversation_ref = MagicMock()
        conversation_ref.get.return_value = MagicMock(
            exists=True, to_dict=lambda: {"data_protection_level": "standard"}
        )
        conversation_ref.collections.return_value = []
        db = MagicMock()
        db.collection.return_value.document.return_value.collection.return_value.document.return_value = (
            conversation_ref
        )
        monkeypatch.setattr(conversations_db, "db", db)
        return conversations_db, index, conversation_ref

    def test_discard_marks_and_syncs(self, conversations_db):
        module, index, _ = conversations_db

        module.set_conversation_as_discarded(UID, CONVERSATION_ID)

        index.sync_conversation_index_after_write.assert_called_once_with(UID, CONVERSATION_ID)

    def test_restore_marks_and_syncs(self, conversations_db):
        module, index, _ = conversations_db

        module.restore_conversation_from_discarded(UID, CONVERSATION_ID)

        index.sync_conversation_index_after_write.assert_called_once_with(UID, CONVERSATION_ID)

    def test_title_edit_syncs(self, conversations_db):
        module, index, _ = conversations_db

        module.update_conversation_title(UID, CONVERSATION_ID, "Renamed")

        index.sync_conversation_index_after_write.assert_called_once_with(UID, CONVERSATION_ID)

    def test_delete_removes_from_index(self, conversations_db):
        module, index, _ = conversations_db

        module.delete_conversation(UID, CONVERSATION_ID)

        index.delete_conversation_index_doc.assert_called_once_with(UID, CONVERSATION_ID)

    def test_structured_update_syncs(self, conversations_db):
        module, index, _ = conversations_db

        assert module.update_conversation(UID, CONVERSATION_ID, {"structured.title": "New"}) is True

        index.sync_conversation_index_after_write.assert_called_once_with(UID, CONVERSATION_ID)

    def test_non_search_update_does_not_sync(self, conversations_db):
        module, index, _ = conversations_db

        assert module.update_conversation(UID, CONVERSATION_ID, {"starred": True}) is True

        index.sync_conversation_index_after_write.assert_not_called()

    def test_typesense_outage_does_not_raise_out_of_write_paths(self, monkeypatch: pytest.MonkeyPatch):
        """The real fail-open path: every Typesense call fails, and neither the
        discard update nor the delete may raise or skip its Firestore write."""

        conversation_ref = MagicMock()
        conversation_ref.get.return_value = MagicMock(
            exists=True, to_dict=lambda: {"data_protection_level": "standard"}
        )
        conversation_ref.collections.return_value = []
        db = MagicMock()
        db.collection.return_value.document.return_value.collection.return_value.document.return_value = (
            conversation_ref
        )
        monkeypatch.setattr(conversations_db, "db", db)
        monkeypatch.setenv("TYPESENSE_HOST", "localhost")
        monkeypatch.setenv("TYPESENSE_API_KEY", "test-key-not-real")
        monkeypatch.delenv(typesense_index.CONVERSATION_INDEX_WRITES_ENV, raising=False)

        broken_client = MagicMock()
        broken_client.collections.__getitem__.return_value.documents.upsert.side_effect = Exception("typesense down")
        broken_client.collections.__getitem__.return_value.documents.__getitem__.side_effect = Exception(
            "typesense down"
        )
        monkeypatch.setattr(typesense_index, "_typesense_client", lambda: broken_client)
        monkeypatch.setattr(typesense_index, "default_db_client", _policy_db())

        conversations_db.set_conversation_as_discarded(UID, CONVERSATION_ID)
        conversations_db.delete_conversation(UID, CONVERSATION_ID)

        # The Firestore writes themselves still landed.
        assert conversation_ref.update.called
        assert conversation_ref.delete.called
