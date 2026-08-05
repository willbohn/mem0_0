from mem0.utils.paths import resolve_mem0_dir


def test_resolve_mem0_dir_prefers_explicit_environment(tmp_path):
    explicit = tmp_path / "custom"

    assert resolve_mem0_dir({"MEM0_DIR": str(explicit)}, home=tmp_path) == explicit


def test_resolve_mem0_dir_uses_new_fork_directory_when_present(tmp_path):
    fork_dir = tmp_path / ".mem0_0"
    legacy_dir = tmp_path / ".mem0"
    fork_dir.mkdir()
    legacy_dir.mkdir()

    assert resolve_mem0_dir({}, home=tmp_path) == fork_dir


def test_resolve_mem0_dir_preserves_legacy_directory_until_explicit_migration(tmp_path):
    legacy_dir = tmp_path / ".mem0"
    legacy_dir.mkdir()

    assert resolve_mem0_dir({}, home=tmp_path) == legacy_dir
    assert not (tmp_path / ".mem0_0").exists()


def test_resolve_mem0_dir_initializes_fresh_installs_under_fork_name(tmp_path):
    assert resolve_mem0_dir({}, home=tmp_path) == tmp_path / ".mem0_0"
