import hashlib
import subprocess

from deployment.mem0_0_arch.source_attestation import TrackedEntry, source_attestation, tracked_tree_fingerprint


def test_tracked_tree_fingerprint_changes_with_content_without_returning_content(tmp_path):
    first = tmp_path / "a.py"
    second = tmp_path / "nested" / "b.py"
    second.parent.mkdir()
    first.write_text("private implementation one", encoding="utf-8")
    second.write_text("private implementation two", encoding="utf-8")

    entries = [
        TrackedEntry("100644", hashlib.sha1(b"ignored").hexdigest(), "a.py"),
        TrackedEntry("100644", hashlib.sha1(b"ignored").hexdigest(), "nested/b.py"),
    ]
    before = tracked_tree_fingerprint(tmp_path, entries)
    first.write_text("changed private implementation", encoding="utf-8")
    after = tracked_tree_fingerprint(tmp_path, reversed(entries))

    assert len(before) == len(after) == 64
    assert before != after
    assert "private" not in before


def test_tracked_tree_fingerprint_represents_uninitialized_gitlink_without_reading_directory(tmp_path):
    (tmp_path / "evaluation").mkdir()
    gitlink = TrackedEntry("160000", "4b61c5d31b9c668a12b4f5e78064248a02c82d2b", "evaluation")

    digest = tracked_tree_fingerprint(tmp_path, [gitlink])

    assert len(digest) == 64


def test_source_attestation_includes_untracked_source_files(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    tracked = tmp_path / "tracked.py"
    untracked = tmp_path / "generated.py"
    tracked.write_text("tracked source", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Mem0 Test",
            "-c",
            "user.email=mem0@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    untracked.write_text("untracked deployed source", encoding="utf-8")

    before = source_attestation(tmp_path)
    untracked.write_text("changed untracked deployed source", encoding="utf-8")
    after = source_attestation(tmp_path)

    assert before["tracked_files"] == 1
    assert before["untracked_files"] == 1
    assert before["clean"] is False
    assert before["source_tree_sha256"] != after["source_tree_sha256"]
