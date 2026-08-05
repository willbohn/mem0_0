import json
from pathlib import Path

import pytest

from evaluation_overlay.qdrant_runtime import QdrantRuntimeError, verify_inspections


def _manifest():
    return {
        "runtimes": {
            "qdrant": {
                "container_name": "mem0_0-benchmark-qdrant",
                "host": "127.0.0.1",
                "image": "docker.io/qdrant/qdrant@sha256:" + "1" * 64,
                "platform": "linux/amd64",
                "platform_digest": "sha256:" + "2" * 64,
                "port": 6333,
                "version": "1.18.3",
            }
        }
    }


def _inspections():
    image = {
        "Architecture": "amd64",
        "Digest": "sha256:" + "2" * 64,
        "Id": "sha256:" + "3" * 64,
        "Os": "linux",
        "RepoDigests": ["docker.io/qdrant/qdrant@sha256:" + "1" * 64],
    }
    container = {
        "Image": image["Id"],
        "ImageName": "docker.io/qdrant/qdrant@sha256:" + "1" * 64,
        "Mounts": [{"Destination": "/qdrant/storage", "Source": "/work/runtime/qdrant/storage"}],
        "Name": "mem0_0-benchmark-qdrant",
        "NetworkSettings": {"Ports": {"6333/tcp": [{"HostIp": "127.0.0.1", "HostPort": "6333"}]}},
        "State": {"Status": "running"},
    }
    return image, container


def test_qdrant_inspection_binds_running_container_to_pinned_image():
    image, container = _inspections()

    verified = verify_inspections(_manifest(), image, container, Path("/work/runtime/qdrant/storage"))

    assert verified["image_id"] == image["Id"]
    assert verified["platform_digest"] == "sha256:" + "2" * 64
    json.dumps(verified)


def test_qdrant_inspection_rejects_container_running_other_image():
    image, container = _inspections()
    container["Image"] = "sha256:" + "9" * 64

    with pytest.raises(QdrantRuntimeError, match="image ID"):
        verify_inspections(_manifest(), image, container, Path("/work/runtime/qdrant/storage"))


def test_qdrant_inspection_accepts_podman_index_digest_with_platform_repo_digest():
    image, container = _inspections()
    image["Digest"] = "sha256:" + "1" * 64
    image["RepoDigests"].append("docker.io/qdrant/qdrant@sha256:" + "2" * 64)

    assert (
        verify_inspections(_manifest(), image, container, Path("/work/runtime/qdrant/storage"))["platform"]
        == "linux/amd64"
    )


def test_qdrant_inspection_rejects_storage_from_another_work_directory():
    image, container = _inspections()

    with pytest.raises(QdrantRuntimeError, match="storage mount"):
        verify_inspections(_manifest(), image, container, Path("/fresh/runtime/qdrant/storage"))
