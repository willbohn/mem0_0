import json
import os
import unittest
import uuid
from unittest.mock import MagicMock, patch

import dotenv
import weaviate

from mem0.exceptions import MemoryPersistenceError
from mem0.memory.main import _discover_lifecycle_candidates
from mem0.vector_stores.weaviate import Weaviate


def packed_metadata(**payload):
    return json.dumps({"_mem0_weaviate_payload_codec": 1, "payload": payload}, sort_keys=True)


class TestWeaviateDB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dotenv.load_dotenv()

        cls.original_env = {
            "WEAVIATE_CLUSTER_URL": os.getenv("WEAVIATE_CLUSTER_URL", "http://localhost:8080"),
            "WEAVIATE_API_KEY": os.getenv("WEAVIATE_API_KEY", "test_api_key"),
        }

        os.environ["WEAVIATE_CLUSTER_URL"] = "http://localhost:8080"
        os.environ["WEAVIATE_API_KEY"] = "test_api_key"

    def setUp(self):
        self.client_mock = MagicMock(spec=weaviate.WeaviateClient)
        self.client_mock.collections = MagicMock()
        self.client_mock.collections.exists.return_value = False
        self.client_mock.collections.create.return_value = None
        self.client_mock.collections.delete.return_value = None

        patcher = patch("mem0.vector_stores.weaviate.weaviate.connect_to_local", return_value=self.client_mock)
        self.mock_weaviate = patcher.start()
        self.addCleanup(patcher.stop)

        self.weaviate_db = Weaviate(
            collection_name="test_collection",
            embedding_model_dims=1536,
            cluster_url=os.getenv("WEAVIATE_CLUSTER_URL"),
            auth_client_secret=os.getenv("WEAVIATE_API_KEY"),
            additional_headers={"X-OpenAI-Api-Key": "test_key"},
        )

        self.client_mock.reset_mock()

    @classmethod
    def tearDownClass(cls):
        for key, value in cls.original_env.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)

    def tearDown(self):
        self.client_mock.reset_mock()

    def test_create_col(self):
        self.client_mock.collections.exists.return_value = False
        self.weaviate_db.create_col(vector_size=1536)

        self.client_mock.collections.create.assert_called_once()

        self.client_mock.reset_mock()

        self.client_mock.collections.exists.return_value = True
        self.weaviate_db.create_col(vector_size=1536)

        self.client_mock.collections.create.assert_not_called()

    def test_insert(self):
        self.client_mock.batch = MagicMock()

        self.client_mock.batch.fixed_size.return_value.__enter__.return_value = MagicMock()

        self.client_mock.collections.get.return_value.data.insert_many.return_value = {
            "results": [{"id": "id1"}, {"id": "id2"}]
        }

        vectors = [[0.1] * 1536, [0.2] * 1536]
        payloads = [{"key1": "value1"}, {"key2": "value2"}]
        ids = [str(uuid.uuid4()), str(uuid.uuid4())]

        self.weaviate_db.insert(vectors=vectors, payloads=payloads, ids=ids)

    def test_insert_packs_non_schema_payload_in_versioned_metadata(self):
        self.client_mock.batch = MagicMock()
        batch = MagicMock()
        self.client_mock.batch.fixed_size.return_value.__enter__.return_value = batch
        payload = {
            "data": "User lives in Tokyo",
            "user_id": "u1",
            "topic": "profile",
            "_mem0_schema_version": 1,
            "_mem0_kind": "state",
            "_mem0_value": {"city": "Tokyo", "confidence": 0.9},
        }

        self.weaviate_db.insert(vectors=[[0.1] * 1536], payloads=[payload], ids=[str(uuid.uuid4())])

        properties = batch.add_object.call_args.kwargs["properties"]
        assert properties["data"] == "User lives in Tokyo"
        assert properties["user_id"] == "u1"
        assert "topic" not in properties
        assert "_mem0_kind" not in properties
        envelope = json.loads(properties["metadata"])
        assert envelope == {
            "_mem0_weaviate_payload_codec": 1,
            "payload": {
                "_mem0_kind": "state",
                "_mem0_schema_version": 1,
                "_mem0_value": {"city": "Tokyo", "confidence": 0.9},
                "topic": "profile",
            },
        }

    def test_get(self):
        valid_uuid = str(uuid.uuid4())

        mock_response = MagicMock()
        mock_response.properties = {
            "hash": "abc123",
            "created_at": "2025-03-08T12:00:00Z",
            "updated_at": "2025-03-08T13:00:00Z",
            "user_id": "user_123",
            "agent_id": "agent_456",
            "run_id": "run_789",
            "data": {"key": "value"},
            "category": "test",
        }
        mock_response.uuid = valid_uuid

        self.client_mock.collections.get.return_value.query.fetch_object_by_id.return_value = mock_response

        result = self.weaviate_db.get(vector_id=valid_uuid)

        assert result.id == valid_uuid

        expected_payload = mock_response.properties.copy()
        expected_payload["id"] = valid_uuid

        assert result.payload == expected_payload

    def test_read_paths_decode_versioned_metadata(self):
        memory_id = str(uuid.uuid4())
        properties = {
            "data": "User lives in Tokyo",
            "user_id": "u1",
            "metadata": packed_metadata(
                topic="profile",
                _mem0_schema_version=1,
                _mem0_kind="state",
                _mem0_state_key="user::residence",
                _mem0_value={"city": "Tokyo"},
                _mem0_status="active",
            ),
        }
        obj = MagicMock()
        obj.uuid = memory_id
        obj.properties = properties
        obj.metadata.distance = 0.2
        obj.metadata.score = 0.8
        response = MagicMock(objects=[obj])
        collection = self.client_mock.collections.get.return_value
        collection.query.hybrid.return_value = response
        collection.query.bm25.return_value = response
        collection.query.fetch_object_by_id.return_value = obj
        collection.query.fetch_objects.return_value = response

        search = self.weaviate_db.search("Tokyo", [0.1] * 1536, filters={"user_id": "u1"})[0]
        keyword = self.weaviate_db.keyword_search("Tokyo", filters={"user_id": "u1"})[0]
        fetched = self.weaviate_db.get(memory_id)
        listed = self.weaviate_db.list(filters={"user_id": "u1"})[0][0]

        for result in (search, keyword, fetched, listed):
            assert result.payload["topic"] == "profile"
            assert result.payload["_mem0_state_key"] == "user::residence"
            assert result.payload["_mem0_value"] == {"city": "Tokyo"}
            assert result.payload["data"] == "User lives in Tokyo"
            assert "metadata" not in result.payload
        assert "return_properties" not in collection.query.hybrid.call_args.kwargs
        assert "return_properties" not in collection.query.bm25.call_args.kwargs
        assert "return_properties" not in collection.query.fetch_object_by_id.call_args.kwargs

    def test_decode_preserves_legacy_metadata_and_uses_documented_merge_order(self):
        legacy_id = str(uuid.uuid4())
        packed_id = str(uuid.uuid4())
        legacy = MagicMock()
        legacy.uuid = legacy_id
        legacy.properties = {
            "data": "Legacy direct data",
            "metadata": "literal legacy metadata",
            "_mem0_status": "active",
        }
        packed = MagicMock()
        packed.uuid = packed_id
        packed.properties = {
            "data": "Direct core data",
            "_mem0_status": "active",
            "metadata": packed_metadata(data="packed data", _mem0_status="superseded", topic="profile"),
        }
        collection = self.client_mock.collections.get.return_value
        collection.query.fetch_object_by_id.side_effect = [legacy, packed]

        legacy_result = self.weaviate_db.get(legacy_id)
        packed_result = self.weaviate_db.get(packed_id)

        assert legacy_result.payload["metadata"] == "literal legacy metadata"
        assert legacy_result.payload["_mem0_status"] == "active"
        assert packed_result.payload["_mem0_status"] == "superseded"
        assert packed_result.payload["topic"] == "profile"
        assert packed_result.payload["data"] == "Direct core data"

    def test_get_not_found(self):
        # fetch_object_by_id returns None for an unknown id; get() must return
        # None rather than raising AttributeError on response.properties.
        self.client_mock.collections.get.return_value.query.fetch_object_by_id.return_value = None

        result = self.weaviate_db.get(vector_id=str(uuid.uuid4()))

        assert result is None

    def test_search(self):
        mock_objects = [{"uuid": "id1", "properties": {"key1": "value1"}, "metadata": {"distance": 0.2}}]

        mock_response = MagicMock()
        mock_response.objects = []

        for obj in mock_objects:
            mock_obj = MagicMock()
            mock_obj.uuid = obj["uuid"]
            mock_obj.properties = obj["properties"]
            mock_obj.metadata = MagicMock()
            mock_obj.metadata.distance = obj["metadata"]["distance"]
            mock_response.objects.append(mock_obj)

        mock_hybrid = MagicMock()
        self.client_mock.collections.get.return_value.query.hybrid = mock_hybrid
        mock_hybrid.return_value = mock_response

        vectors = [[0.1] * 1536]
        results = self.weaviate_db.search(query="", vectors=vectors, top_k=5)

        mock_hybrid.assert_called_once()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].id, "id1")
        self.assertEqual(results[0].score, 0.8)

    def test_delete(self):
        self.weaviate_db.delete(vector_id="id1")

        self.client_mock.collections.get.return_value.data.delete_by_id.assert_called_once_with("id1")

    def test_update_packs_non_schema_payload_in_versioned_metadata(self):
        collection = self.client_mock.collections.get.return_value

        self.weaviate_db.update(
            "state-id",
            payload={
                "data": "User lives in Tokyo",
                "user_id": "u1",
                "topic": "profile",
                "_mem0_schema_version": 1,
                "_mem0_status": "superseded",
            },
        )

        properties = collection.data.update.call_args.kwargs["properties"]
        assert properties["data"] == "User lives in Tokyo"
        assert properties["user_id"] == "u1"
        assert "topic" not in properties
        assert "_mem0_status" not in properties
        envelope = json.loads(properties["metadata"])
        assert envelope["_mem0_weaviate_payload_codec"] == 1
        assert envelope["payload"] == {
            "_mem0_schema_version": 1,
            "_mem0_status": "superseded",
            "topic": "profile",
        }

    def test_list(self):
        mock_objects = []

        mock_obj1 = MagicMock()
        mock_obj1.uuid = "id1"
        mock_obj1.properties = {"key1": "value1"}
        mock_objects.append(mock_obj1)

        mock_obj2 = MagicMock()
        mock_obj2.uuid = "id2"
        mock_obj2.properties = {"key2": "value2"}
        mock_objects.append(mock_obj2)

        mock_response = MagicMock()
        mock_response.objects = mock_objects

        mock_fetch = MagicMock()
        self.client_mock.collections.get.return_value.query.fetch_objects = mock_fetch
        mock_fetch.return_value = mock_response

        results = self.weaviate_db.list(top_k=10)

        mock_fetch.assert_called_once()

        # Verify results
        self.assertEqual(len(results), 1)
        self.assertEqual(len(results[0]), 2)
        self.assertEqual(results[0][0].id, "id1")
        self.assertEqual(results[0][0].payload["key1"], "value1")
        self.assertEqual(results[0][1].id, "id2")
        self.assertEqual(results[0][1].payload["key2"], "value2")

    def test_list_returns_stored_lifecycle_metadata_without_projection(self):
        mock_obj = MagicMock()
        mock_obj.uuid = "state-id"
        mock_obj.properties = {
            "data": "User lives in Tokyo",
            "user_id": "u1",
            "_mem0_schema_version": 1,
            "_mem0_kind": "state",
            "_mem0_state_key": "user::residence",
            "_mem0_value": "Tokyo",
            "_mem0_status": "active",
        }
        mock_response = MagicMock(objects=[mock_obj])
        mock_fetch = self.client_mock.collections.get.return_value.query.fetch_objects
        mock_fetch.return_value = mock_response

        results = self.weaviate_db.list(filters={"user_id": "u1"}, top_k=10)

        assert results[0][0].payload["_mem0_schema_version"] == 1
        assert results[0][0].payload["_mem0_state_key"] == "user::residence"
        assert "return_properties" not in mock_fetch.call_args.kwargs

    def test_lifecycle_candidate_discovery_rejects_incomplete_enumeration_capability(self):
        with self.assertRaisesRegex(MemoryPersistenceError, "complete-list capability"):
            _discover_lifecycle_candidates(self.weaviate_db, {"user_id": "u1"})

        self.client_mock.collections.get.assert_not_called()

    def test_list_cols(self):
        mock_collection1 = MagicMock()
        mock_collection1.name = "collection1"

        mock_collection2 = MagicMock()
        mock_collection2.name = "collection2"
        self.client_mock.collections.list_all.return_value = [mock_collection1, mock_collection2]

        result = self.weaviate_db.list_cols()
        expected = {"collections": [{"name": "collection1"}, {"name": "collection2"}]}

        assert result == expected

        self.client_mock.collections.list_all.assert_called_once()

    def test_list_cols_does_not_print(self):
        mock_collection = MagicMock()
        mock_collection.name = "collection1"
        self.client_mock.collections.list_all.return_value = [mock_collection]

        with patch("builtins.print") as mock_print:
            self.weaviate_db.list_cols()

        mock_print.assert_not_called()

    def test_delete_col(self):
        self.weaviate_db.delete_col()

        self.client_mock.collections.delete.assert_called_once_with("test_collection")

    def test_reset(self):
        self.client_mock.collections.exists.return_value = False

        self.weaviate_db.reset()

        self.client_mock.collections.delete.assert_called_once_with("test_collection")
        self.client_mock.collections.create.assert_called_once()

    def test_update_preserves_properties_and_does_not_write_model_fields(self):
        # Regression: the vector branch of update() previously resent
        # dict(OutputData) as properties, i.e. the model field names
        # {"id", "score", "payload"}, corrupting the stored object instead of
        # preserving its real properties.
        valid_uuid = str(uuid.uuid4())
        collection = self.client_mock.collections.get.return_value

        # Mock get() so that, on the buggy code path, the vector branch can run
        # to completion and actually write its (wrong) properties.
        mock_response = MagicMock()
        mock_response.properties = {"data": "existing", "hash": "abc123"}
        mock_response.uuid = valid_uuid
        collection.query.fetch_object_by_id.return_value = mock_response

        new_payload = {
            "data": "updated memory",
            "hash": "def456",
            "user_id": "user_123",
        }

        self.weaviate_db.update(
            vector_id=valid_uuid,
            vector=[0.1] * 1536,
            payload=new_payload,
        )

        update_calls = collection.data.update.call_args_list
        # The payload branch updates the real properties.
        property_updates = [c for c in update_calls if "properties" in c.kwargs]
        self.assertEqual(len(property_updates), 1)
        self.assertEqual(property_updates[0].kwargs["properties"], new_payload)

        # No update call may write the OutputData model field names as properties.
        for call in update_calls:
            props = call.kwargs.get("properties", {})
            self.assertNotIn("score", props)
            self.assertNotIn("payload", props)

        # The vector branch updates the vector without touching properties.
        vector_updates = [c for c in update_calls if "vector" in c.kwargs]
        self.assertEqual(len(vector_updates), 1)
        self.assertNotIn("properties", vector_updates[0].kwargs)


if __name__ == "__main__":
    unittest.main()
