import pytest

from deployment.mem0_0_arch.shadow_runtime import ShadowRuntimeError, start_postgres, start_shadow


class FakeRunner:
    def __init__(self, labels: str, running_image: str | None = None):
        self.labels = labels
        self.running_image = running_image or f"localhost/mem0_0-arch:{'a' * 40}"
        self.commands = []

    def run(self, command, **kwargs):
        self.commands.append(tuple(command))
        if command[0:3] == ["podman", "image", "inspect"]:
            return self.labels.encode()
        if command[0:2] == ["podman", "inspect"]:
            return self.running_image.encode()
        return b""


def _attestation():
    return {"revision": "a" * 40, "source_tree_sha256": "b" * 64, "clean": False}


def _environment():
    return {
        "MEM0_0_SOURCE_REVISION": "a" * 40,
        "MEM0_0_SOURCE_TREE_SHA256": "b" * 64,
        "MEM0_0_PYTHON_IMAGE": f"python@sha256:{'e' * 64}",
        "MEM0_0_PGVECTOR_IMAGE": f"pgvector@sha256:{'f' * 64}",
    }


def test_shadow_start_builds_then_rechecks_and_uses_only_labeled_image(tmp_path):
    runner = FakeRunner(f"{'a' * 40}|{'b' * 64}")
    reports = [_attestation(), _attestation()]

    report = start_shadow(
        repository=tmp_path,
        compose_file=tmp_path / "compose.yaml",
        runner=runner,
        environ=_environment(),
        attestor=lambda repository: reports.pop(0),
    )

    assert report["status"] == "started"
    assert any("build" in command for command in runner.commands)
    assert runner.commands[-2][-5:] == ("up", "-d", "--no-build", "--force-recreate", "mem0")
    assert runner.commands[-1][-1] == "mem0_0-arch-shadow-mem0-1"


def test_shadow_start_fails_when_compose_does_not_launch_the_attested_image(tmp_path):
    runner = FakeRunner(f"{'a' * 40}|{'b' * 64}", running_image="localhost/mem0_0-arch:stale")

    with pytest.raises(ShadowRuntimeError, match="running shadow image"):
        start_shadow(
            repository=tmp_path,
            compose_file=tmp_path / "compose.yaml",
            runner=runner,
            environ=_environment(),
            attestor=lambda repository: _attestation(),
        )


def test_shadow_start_never_builds_when_source_does_not_match_pins(tmp_path):
    runner = FakeRunner("")

    with pytest.raises(ShadowRuntimeError, match="does not match"):
        start_shadow(
            repository=tmp_path,
            compose_file=tmp_path / "compose.yaml",
            runner=runner,
            environ=_environment(),
            attestor=lambda repository: {"revision": "c" * 40, "source_tree_sha256": "b" * 64},
        )

    assert runner.commands == []


def test_shadow_start_never_runs_image_when_labels_do_not_match(tmp_path):
    runner = FakeRunner(f"{'a' * 40}|{'c' * 64}")

    with pytest.raises(ShadowRuntimeError, match="image labels"):
        start_shadow(
            repository=tmp_path,
            compose_file=tmp_path / "compose.yaml",
            runner=runner,
            environ=_environment(),
            attestor=lambda repository: _attestation(),
        )

    assert not any("up" in command for command in runner.commands)


def test_postgres_start_requires_digest_before_compose_mutation(tmp_path):
    runner = FakeRunner("")

    with pytest.raises(ShadowRuntimeError, match="MEM0_0_PGVECTOR_IMAGE"):
        start_postgres(
            compose_file=tmp_path / "compose.yaml",
            runner=runner,
            environ={"MEM0_0_PGVECTOR_IMAGE": "pgvector:pg17"},
        )

    assert runner.commands == []


def test_postgres_start_accepts_only_pinned_image(tmp_path):
    runner = FakeRunner("")

    report = start_postgres(
        compose_file=tmp_path / "compose.yaml",
        runner=runner,
        environ=_environment(),
    )

    assert report["image"].endswith("f" * 64)
    assert runner.commands[-1][-3:] == ("up", "-d", "postgres")
