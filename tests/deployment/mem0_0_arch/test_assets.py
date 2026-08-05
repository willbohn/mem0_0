from pathlib import Path


ROOT = Path("deployment/mem0_0_arch")


def test_shadow_compose_uses_isolated_ports_state_and_volume_with_digest_pins():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "28889" in compose
    assert "25432" in compose
    assert "/home/will/.local/share/mem0_0-arch/runtime:/app/state" in compose
    assert "mem0_0_arch_shadow_postgres_data" in compose
    assert "MEM0_0_PGVECTOR_IMAGE:?" in compose
    assert "/home/will/.local/share/mem0-arch" not in compose
    assert "15432" not in compose
    assert "8888" not in compose


def test_systemd_unit_targets_repository_owned_compose_and_never_deletes_state():
    unit = (ROOT / "systemd" / "mem0_0-arch.service").read_text(encoding="utf-8")

    assert "CodingWorkspaces/mem0_0" in unit
    assert "deployment/mem0_0_arch/compose.yaml" in unit
    assert "mem0-implement" not in unit
    assert "rm " not in unit


def test_deployment_environment_template_contains_no_filled_credentials():
    template = (ROOT / "deployment.env.example").read_text(encoding="utf-8")

    assert "PASSWORD=" not in template
    assert "API_KEY=" not in template
    assert "@sha256:<digest>" in template


def test_container_build_does_not_copy_ignored_server_secrets():
    containerfile = (ROOT / "Containerfile").read_text(encoding="utf-8")

    assert "COPY server /app" not in containerfile
    assert "COPY server/*.py server/alembic.ini /app/" in containerfile
    assert "COPY server/alembic /app/alembic" in containerfile
    assert "COPY server/routers /app/routers" in containerfile
    ignorefile = (ROOT / "containerignore").read_text(encoding="utf-8")
    assert "**/.env" in ignorefile
    assert "**/__pycache__/" in ignorefile
