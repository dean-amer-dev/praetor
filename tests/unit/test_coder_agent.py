"""Unit tests for agents/coder/agent.py — file ops and sandbox enforcement."""
import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _reset_scratch(scratch_dir, monkeypatch):
    """scratch_dir fixture sets SCRATCH_DIR and patches coder_agent.SCRATCH_DIR."""
    pass  # fixture side effects handle the setup


class TestScratchPathSafety:
    def test_normal_path_resolves_inside_scratch(self, scratch_dir):
        from agents.coder.agent import _scratch
        result = _scratch("foo/bar.py")
        assert result.startswith(str(scratch_dir))
        assert "foo/bar.py" in result

    def test_double_dot_traversal_blocked(self, scratch_dir):
        from agents.coder.agent import _scratch
        with pytest.raises(ValueError, match="path traversal"):
            _scratch("../../etc/passwd")

    def test_absolute_path_blocked(self, scratch_dir):
        from agents.coder.agent import _scratch
        with pytest.raises(ValueError, match="path traversal"):
            _scratch("/etc/passwd")

    def test_tricky_traversal_blocked(self, scratch_dir):
        from agents.coder.agent import _scratch
        with pytest.raises(ValueError, match="path traversal"):
            _scratch("subdir/../../etc/shadow")


class TestWriteFile:
    def test_write_creates_file(self, scratch_dir):
        from agents.coder.agent import write_file
        write_file("hello.txt", "world")
        assert (scratch_dir / "hello.txt").read_text() == "world"

    def test_write_creates_parent_dirs(self, scratch_dir):
        from agents.coder.agent import write_file
        write_file("deep/nested/dir/file.py", "# code")
        assert (scratch_dir / "deep/nested/dir/file.py").exists()

    def test_write_returns_confirmation(self, scratch_dir):
        from agents.coder.agent import write_file
        result = write_file("a.txt", "x")
        assert "a.txt" in result

    def test_write_overwrites_existing(self, scratch_dir):
        from agents.coder.agent import write_file
        write_file("f.txt", "old")
        write_file("f.txt", "new")
        assert (scratch_dir / "f.txt").read_text() == "new"


class TestReadFile:
    async def test_read_returns_content(self, scratch_dir):
        from agents.coder.agent import read_file, write_file
        (scratch_dir / ".git").mkdir()  # simulate cloned repo
        write_file("r.txt", "hello read")
        assert await read_file("r.txt") == "hello read"

    async def test_read_handles_binary_gracefully(self, scratch_dir):
        from agents.coder.agent import read_file
        (scratch_dir / ".git").mkdir()  # simulate cloned repo
        (scratch_dir / "bin.dat").write_bytes(b"\xff\xfe\x00\x01")
        result = await read_file("bin.dat")
        assert isinstance(result, str)  # no UnicodeDecodeError

    async def test_read_before_clone_returns_error(self, scratch_dir):
        from agents.coder.agent import read_file
        result = await read_file("any_file.py")
        assert "not cloned yet" in result  # guard message triggers


class TestRunShell:
    async def test_returns_stdout(self, scratch_dir):
        from agents.coder.agent import run_shell
        result = await run_shell("echo hello")
        assert "hello" in result

    async def test_captures_stderr(self, scratch_dir):
        from agents.coder.agent import run_shell
        result = await run_shell("ls /nonexistent_path_xyz 2>&1 || true")
        assert isinstance(result, str)

    async def test_cwd_is_scratch(self, scratch_dir):
        from agents.coder.agent import run_shell
        result = await run_shell("pwd")
        assert str(scratch_dir) in result

    async def test_nonzero_exit_returns_exit_marker(self, scratch_dir):
        from agents.coder.agent import run_shell
        result = await run_shell("exit 1")
        # No output + nonzero → "(exit 1)"
        assert "1" in result

    async def test_path_traversal_in_cmd_blocked(self, scratch_dir):
        from agents.coder.agent import run_shell
        with pytest.raises(ValueError, match="path traversal"):
            await run_shell("cat ../../etc/passwd")

    async def test_creates_scratch_dir_if_missing(self, tmp_path, monkeypatch):
        """run_shell creates SCRATCH_DIR if it doesn't exist yet."""
        import agents.coder.agent as coder_agent
        new_scratch = tmp_path / "new_scratch"
        monkeypatch.setattr(coder_agent, "SCRATCH_DIR", str(new_scratch))
        from agents.coder.agent import run_shell
        result = await run_shell("echo ok")
        assert new_scratch.exists()
        assert "ok" in result
