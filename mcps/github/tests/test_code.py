"""Tests for code and content operations."""

import httpx
import pytest
import respx

from github.github_client import GITHUB_API_BASE_URL, AsyncGitHubClient, GitHubClient
from .conftest import (
    SAMPLE_FILE_CONTENT,
    SAMPLE_DIRECTORY_LISTING,
    SAMPLE_FILE_CREATE_RESULT,
    SAMPLE_CODE_SEARCH_RESPONSE,
    SAMPLE_USER,
)


class TestCodeSync:
    """Synchronous code operation tests."""

    @respx.mock
    def test_get_file_content(self):
        from github.code import get_file_content

        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/contents/src/main.py").mock(
            return_value=httpx.Response(200, json=SAMPLE_FILE_CONTENT)
        )
        with GitHubClient("tok") as client:
            result = get_file_content(client, "o", "r", "src/main.py")

        assert result["name"] == "main.py"
        assert "decoded_content" in result
        assert "import sys" in result["decoded_content"]

    @respx.mock
    def test_get_file_content_with_ref(self):
        from github.code import get_file_content

        route = respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/contents/src/main.py").mock(
            return_value=httpx.Response(200, json=SAMPLE_FILE_CONTENT)
        )
        with GitHubClient("tok") as client:
            get_file_content(client, "o", "r", "src/main.py", ref="v1.0")

        assert "ref=v1.0" in str(route.calls[0].request.url)

    @respx.mock
    def test_get_directory_listing(self):
        from github.code import get_file_content

        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/contents/src").mock(
            return_value=httpx.Response(200, json=SAMPLE_DIRECTORY_LISTING)
        )
        with GitHubClient("tok") as client:
            result = get_file_content(client, "o", "r", "src")

        # Directory listings return a list, not a dict
        assert isinstance(result, list)
        assert len(result) == 3

    @respx.mock
    def test_create_or_update_file(self):
        from github.code import create_or_update_file

        route = respx.put(f"{GITHUB_API_BASE_URL}/repos/o/r/contents/new-file.txt").mock(
            return_value=httpx.Response(201, json=SAMPLE_FILE_CREATE_RESULT)
        )
        with GitHubClient("tok") as client:
            result = create_or_update_file(
                client, "o", "r", "new-file.txt",
                content="Hello World", message="Create new-file.txt",
            )

        assert result["commit"]["sha"] == "commit456"
        assert route.called

    @respx.mock
    def test_create_or_update_file_with_sha(self):
        from github.code import create_or_update_file
        import json

        route = respx.put(f"{GITHUB_API_BASE_URL}/repos/o/r/contents/existing.txt").mock(
            return_value=httpx.Response(200, json=SAMPLE_FILE_CREATE_RESULT)
        )
        with GitHubClient("tok") as client:
            create_or_update_file(
                client, "o", "r", "existing.txt",
                content="Updated", message="Update file", sha="old123",
            )

        body = json.loads(route.calls[0].request.content)
        assert body["sha"] == "old123"

    @respx.mock
    def test_search_code(self):
        from github.code import search_code

        respx.get(f"{GITHUB_API_BASE_URL}/search/code").mock(
            return_value=httpx.Response(200, json=SAMPLE_CODE_SEARCH_RESPONSE)
        )
        with GitHubClient("tok") as client:
            result = search_code(client, query="addClass")

        assert len(result) == 2
        assert result[0]["name"] == "main.py"

    @respx.mock
    def test_get_authenticated_user(self):
        from github.code import get_authenticated_user

        respx.get(f"{GITHUB_API_BASE_URL}/user").mock(
            return_value=httpx.Response(200, json=SAMPLE_USER)
        )
        with GitHubClient("tok") as client:
            result = get_authenticated_user(client)

        assert result["login"] == "octocat"
        assert result["name"] == "The Octocat"


class TestCodeAsync:
    """Asynchronous code operation tests."""

    @respx.mock
    async def test_aget_file_content(self):
        from github.code import aget_file_content

        respx.get(f"{GITHUB_API_BASE_URL}/repos/o/r/contents/src/main.py").mock(
            return_value=httpx.Response(200, json=SAMPLE_FILE_CONTENT)
        )
        async with AsyncGitHubClient("tok") as client:
            result = await aget_file_content(client, "o", "r", "src/main.py")

        assert result["name"] == "main.py"
        assert "decoded_content" in result

    @respx.mock
    async def test_acreate_or_update_file(self):
        from github.code import acreate_or_update_file

        respx.put(f"{GITHUB_API_BASE_URL}/repos/o/r/contents/new.txt").mock(
            return_value=httpx.Response(201, json=SAMPLE_FILE_CREATE_RESULT)
        )
        async with AsyncGitHubClient("tok") as client:
            result = await acreate_or_update_file(
                client, "o", "r", "new.txt",
                content="Hello", message="Create",
            )

        assert result["commit"]["sha"] == "commit456"

    @respx.mock
    async def test_asearch_code(self):
        from github.code import asearch_code

        respx.get(f"{GITHUB_API_BASE_URL}/search/code").mock(
            return_value=httpx.Response(200, json=SAMPLE_CODE_SEARCH_RESPONSE)
        )
        async with AsyncGitHubClient("tok") as client:
            result = await asearch_code(client, query="test")

        assert len(result) == 2

    @respx.mock
    async def test_aget_authenticated_user(self):
        from github.code import aget_authenticated_user

        respx.get(f"{GITHUB_API_BASE_URL}/user").mock(
            return_value=httpx.Response(200, json=SAMPLE_USER)
        )
        async with AsyncGitHubClient("tok") as client:
            result = await aget_authenticated_user(client)

        assert result["login"] == "octocat"
