"""Tests for Docker delegation logic in the CLI."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

from mailsort.main import _is_docker_container_running, _maybe_delegate_to_docker


def test_docker_not_running_when_docker_missing():
    """If docker CLI is not installed, _is_docker_container_running returns False."""
    with patch("mailsort.main.subprocess.run", side_effect=FileNotFoundError):
        assert _is_docker_container_running() is False


def test_docker_not_running_when_container_absent():
    """If docker inspect fails (container doesn't exist), returns False."""
    mock_result = MagicMock(returncode=1, stdout="")
    with patch("mailsort.main.subprocess.run", return_value=mock_result):
        assert _is_docker_container_running() is False


def test_docker_running_when_container_active():
    """If docker inspect returns 'true', container is running."""
    mock_result = MagicMock(returncode=0, stdout="true\n")
    with patch("mailsort.main.subprocess.run", return_value=mock_result):
        assert _is_docker_container_running() is True


def test_docker_not_running_when_container_stopped():
    """If docker inspect returns 'false', container is stopped."""
    mock_result = MagicMock(returncode=0, stdout="false\n")
    with patch("mailsort.main.subprocess.run", return_value=mock_result):
        assert _is_docker_container_running() is False


def test_delegate_returns_false_when_no_docker():
    """_maybe_delegate_to_docker returns False when no container is running."""
    with patch("mailsort.main._is_docker_container_running", return_value=False):
        assert _maybe_delegate_to_docker(["run"]) is False


def test_delegate_calls_docker_exec(monkeypatch):
    """_maybe_delegate_to_docker calls docker exec and exits with its code."""
    mock_exec = MagicMock(returncode=0)

    with patch("mailsort.main._is_docker_container_running", return_value=True):
        with patch("mailsort.main.subprocess.run", return_value=mock_exec) as mock_run:
            with patch("mailsort.main.sys.exit") as mock_exit:
                _maybe_delegate_to_docker(["run"])

                mock_run.assert_called_once_with(
                    ["docker", "exec", "mailsort", "mailsort", "run"],
                )
                mock_exit.assert_called_once_with(0)
