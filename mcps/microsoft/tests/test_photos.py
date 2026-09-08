"""Tests for ms_graph.photos — profile photo metadata and bytes."""

import httpx
import pytest
import respx
from ms_graph import photos
from ms_graph.graph_client import GRAPH_BASE_URL, AsyncGraphClient, GraphClient, GraphError
from ms_graph.people import DirectoryScopeMissingError
from ms_graph.photos import (
    DEFAULT_PHOTO_SIZE,
    ORIGINAL,
    PHOTO_SIZES,
    _value_path,
    check_photo_size,
    photo_base,
)

from .conftest import GRAPH_ERROR_403, GRAPH_ERROR_404, SAMPLE_PHOTO_METADATA

USER_ID = "user-id-002"
ME_PHOTO_URL = f"{GRAPH_BASE_URL}/me/photo"
ME_PHOTO_240_URL = f"{GRAPH_BASE_URL}/me/photos/240x240/$value"
ME_PHOTO_ORIGINAL_URL = f"{GRAPH_BASE_URL}/me/photo/$value"
USER_PHOTO_URL = f"{GRAPH_BASE_URL}/users/{USER_ID}/photo"
USER_PHOTO_96_URL = f"{GRAPH_BASE_URL}/users/{USER_ID}/photos/96x96/$value"
PHOTO_BYTES = b"\x89PNGfake"
GRAPH_ERROR_500 = {"error": {"code": "InternalServerError", "message": "boom"}}


class TestPhotoBase:
    def test_no_user_is_me(self):
        assert photo_base() == "/me"

    def test_a_user_is_the_quoted_users_path(self):
        assert photo_base("a/b") == "/users/a%2Fb"


class TestCheckPhotoSize:
    def test_empty_is_the_default_size(self):
        assert check_photo_size("") == DEFAULT_PHOTO_SIZE

    def test_whitespace_only_is_the_default_size(self):
        assert check_photo_size("   ") == DEFAULT_PHOTO_SIZE

    @pytest.mark.parametrize("size", PHOTO_SIZES)
    def test_every_fixed_size_passes_unchanged(self, size):
        assert check_photo_size(size) == size

    def test_original_passes_unchanged(self):
        assert check_photo_size(ORIGINAL) == ORIGINAL

    def test_uppercase_original_is_normalised_not_rejected(self):
        """The tool lowercases too; a case difference is never a user error."""
        assert check_photo_size("ORIGINAL") == ORIGINAL

    def test_surrounding_space_and_case_are_stripped(self):
        assert check_photo_size(" 96X96 ") == "96x96"

    def test_a_size_graph_does_not_serve_is_rejected(self):
        with pytest.raises(ValueError) as exc_info:
            check_photo_size("100x100")

        message = str(exc_info.value)
        assert "48x48" in message
        assert "original" in message
        assert "100x100" in message

    def test_a_word_that_is_not_a_size_is_rejected(self):
        with pytest.raises(ValueError) as exc_info:
            check_photo_size("big")

        assert "48x48" in str(exc_info.value)
        assert "original" in str(exc_info.value)


class TestValuePath:
    def test_a_fixed_size_uses_the_photos_collection(self):
        assert _value_path("", "240x240") == "/me/photos/240x240/$value"

    def test_original_uses_the_photo_singleton(self):
        assert _value_path("", ORIGINAL) == "/me/photo/$value"

    def test_a_user_size_path(self):
        assert _value_path(USER_ID, "96x96") == f"/users/{USER_ID}/photos/96x96/$value"


class TestGetPhotoMetadata:
    @respx.mock
    def test_200_is_the_graph_payload(self):
        respx.get(ME_PHOTO_URL).mock(return_value=httpx.Response(200, json=SAMPLE_PHOTO_METADATA))
        with GraphClient("tok") as client:
            assert photos.get_photo_metadata(client) == SAMPLE_PHOTO_METADATA

    @respx.mock
    def test_404_means_no_photo_set(self):
        respx.get(ME_PHOTO_URL).mock(return_value=httpx.Response(404, json=GRAPH_ERROR_404))
        with GraphClient("tok") as client:
            assert photos.get_photo_metadata(client) is None

    @respx.mock
    def test_a_user_reads_the_users_path(self):
        route = respx.get(USER_PHOTO_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_PHOTO_METADATA)
        )
        with GraphClient("tok") as client:
            photos.get_photo_metadata(client, USER_ID)

        assert route.calls[0].request.url.path == f"/v1.0/users/{USER_ID}/photo"

    @respx.mock
    def test_403_on_a_user_is_the_missing_directory_scope(self):
        respx.get(USER_PHOTO_URL).mock(return_value=httpx.Response(403, json=GRAPH_ERROR_403))
        with GraphClient("tok") as client:
            with pytest.raises(DirectoryScopeMissingError):
                photos.get_photo_metadata(client, USER_ID)

    @respx.mock
    def test_403_on_my_own_photo_is_also_the_directory_scope_error(self):
        """A tenant policy can 403 /me/photo; it must collapse to one error."""
        respx.get(ME_PHOTO_URL).mock(return_value=httpx.Response(403, json=GRAPH_ERROR_403))
        with GraphClient("tok") as client:
            with pytest.raises(DirectoryScopeMissingError):
                photos.get_photo_metadata(client)

    @respx.mock
    def test_500_propagates_as_a_graph_error(self):
        respx.get(ME_PHOTO_URL).mock(return_value=httpx.Response(500, json=GRAPH_ERROR_500))
        with GraphClient("tok") as client:
            with pytest.raises(GraphError) as exc_info:
                photos.get_photo_metadata(client)

        assert exc_info.value.status_code == 500


class TestAgetPhotoMetadata:
    @respx.mock
    async def test_200_is_the_graph_payload(self):
        respx.get(ME_PHOTO_URL).mock(return_value=httpx.Response(200, json=SAMPLE_PHOTO_METADATA))
        async with AsyncGraphClient("tok") as client:
            assert await photos.aget_photo_metadata(client) == SAMPLE_PHOTO_METADATA

    @respx.mock
    async def test_404_means_no_photo_set(self):
        respx.get(ME_PHOTO_URL).mock(return_value=httpx.Response(404, json=GRAPH_ERROR_404))
        async with AsyncGraphClient("tok") as client:
            assert await photos.aget_photo_metadata(client) is None

    @respx.mock
    async def test_a_user_reads_the_users_path(self):
        route = respx.get(USER_PHOTO_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_PHOTO_METADATA)
        )
        async with AsyncGraphClient("tok") as client:
            await photos.aget_photo_metadata(client, USER_ID)

        assert route.calls[0].request.url.path == f"/v1.0/users/{USER_ID}/photo"

    @respx.mock
    async def test_403_on_a_user_is_the_missing_directory_scope(self):
        respx.get(USER_PHOTO_URL).mock(return_value=httpx.Response(403, json=GRAPH_ERROR_403))
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(DirectoryScopeMissingError):
                await photos.aget_photo_metadata(client, USER_ID)

    @respx.mock
    async def test_403_on_my_own_photo_is_also_the_directory_scope_error(self):
        respx.get(ME_PHOTO_URL).mock(return_value=httpx.Response(403, json=GRAPH_ERROR_403))
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(DirectoryScopeMissingError):
                await photos.aget_photo_metadata(client)

    @respx.mock
    async def test_500_propagates_as_a_graph_error(self):
        respx.get(ME_PHOTO_URL).mock(return_value=httpx.Response(500, json=GRAPH_ERROR_500))
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(GraphError) as exc_info:
                await photos.aget_photo_metadata(client)

        assert exc_info.value.status_code == 500


class TestGetPhotoBytes:
    @respx.mock
    def test_the_default_size_is_240(self):
        route = respx.get(ME_PHOTO_240_URL).mock(
            return_value=httpx.Response(
                200, content=PHOTO_BYTES, headers={"Content-Type": "image/jpeg"}
            )
        )
        with GraphClient("tok") as client:
            assert photos.get_photo_bytes(client) == (PHOTO_BYTES, "image/jpeg")

        assert route.calls[0].request.url.path == "/v1.0/me/photos/240x240/$value"

    @respx.mock
    def test_original_reads_the_photo_singleton(self):
        route = respx.get(ME_PHOTO_ORIGINAL_URL).mock(
            return_value=httpx.Response(
                200, content=PHOTO_BYTES, headers={"Content-Type": "image/png"}
            )
        )
        with GraphClient("tok") as client:
            photos.get_photo_bytes(client, size=ORIGINAL)

        assert route.calls[0].request.url.path == "/v1.0/me/photo/$value"

    @respx.mock
    def test_a_user_at_a_size(self):
        route = respx.get(USER_PHOTO_96_URL).mock(
            return_value=httpx.Response(
                200, content=PHOTO_BYTES, headers={"Content-Type": "image/png"}
            )
        )
        with GraphClient("tok") as client:
            assert photos.get_photo_bytes(client, USER_ID, "96x96") == (PHOTO_BYTES, "image/png")

        assert route.calls[0].request.url.path == f"/v1.0/users/{USER_ID}/photos/96x96/$value"

    @respx.mock
    def test_404_means_no_photo_set(self):
        respx.get(ME_PHOTO_240_URL).mock(return_value=httpx.Response(404, json=GRAPH_ERROR_404))
        with GraphClient("tok") as client:
            assert photos.get_photo_bytes(client) is None

    @respx.mock
    def test_403_on_a_user_is_the_missing_directory_scope(self):
        respx.get(USER_PHOTO_96_URL).mock(return_value=httpx.Response(403, json=GRAPH_ERROR_403))
        with GraphClient("tok") as client:
            with pytest.raises(DirectoryScopeMissingError):
                photos.get_photo_bytes(client, USER_ID, "96x96")

    @respx.mock
    def test_403_on_my_own_photo_is_also_the_directory_scope_error(self):
        respx.get(ME_PHOTO_240_URL).mock(return_value=httpx.Response(403, json=GRAPH_ERROR_403))
        with GraphClient("tok") as client:
            with pytest.raises(DirectoryScopeMissingError):
                photos.get_photo_bytes(client)

    @respx.mock
    def test_500_propagates_as_a_graph_error(self):
        respx.get(ME_PHOTO_240_URL).mock(return_value=httpx.Response(500, json=GRAPH_ERROR_500))
        with GraphClient("tok") as client:
            with pytest.raises(GraphError) as exc_info:
                photos.get_photo_bytes(client)

        assert exc_info.value.status_code == 500

    @respx.mock
    def test_a_bad_size_is_rejected_before_any_request(self):
        with GraphClient("tok") as client:
            with pytest.raises(ValueError):
                photos.get_photo_bytes(client, size="100x100")

        assert respx.calls.call_count == 0


class TestAgetPhotoBytes:
    @respx.mock
    async def test_the_default_size_is_240(self):
        route = respx.get(ME_PHOTO_240_URL).mock(
            return_value=httpx.Response(
                200, content=PHOTO_BYTES, headers={"Content-Type": "image/jpeg"}
            )
        )
        async with AsyncGraphClient("tok") as client:
            assert await photos.aget_photo_bytes(client) == (PHOTO_BYTES, "image/jpeg")

        assert route.calls[0].request.url.path == "/v1.0/me/photos/240x240/$value"

    @respx.mock
    async def test_original_reads_the_photo_singleton(self):
        route = respx.get(ME_PHOTO_ORIGINAL_URL).mock(
            return_value=httpx.Response(
                200, content=PHOTO_BYTES, headers={"Content-Type": "image/png"}
            )
        )
        async with AsyncGraphClient("tok") as client:
            await photos.aget_photo_bytes(client, size=ORIGINAL)

        assert route.calls[0].request.url.path == "/v1.0/me/photo/$value"

    @respx.mock
    async def test_a_user_at_a_size(self):
        route = respx.get(USER_PHOTO_96_URL).mock(
            return_value=httpx.Response(
                200, content=PHOTO_BYTES, headers={"Content-Type": "image/png"}
            )
        )
        async with AsyncGraphClient("tok") as client:
            found = await photos.aget_photo_bytes(client, USER_ID, "96x96")

        assert found == (PHOTO_BYTES, "image/png")
        assert route.calls[0].request.url.path == f"/v1.0/users/{USER_ID}/photos/96x96/$value"

    @respx.mock
    async def test_404_means_no_photo_set(self):
        respx.get(ME_PHOTO_240_URL).mock(return_value=httpx.Response(404, json=GRAPH_ERROR_404))
        async with AsyncGraphClient("tok") as client:
            assert await photos.aget_photo_bytes(client) is None

    @respx.mock
    async def test_403_on_a_user_is_the_missing_directory_scope(self):
        respx.get(USER_PHOTO_96_URL).mock(return_value=httpx.Response(403, json=GRAPH_ERROR_403))
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(DirectoryScopeMissingError):
                await photos.aget_photo_bytes(client, USER_ID, "96x96")

    @respx.mock
    async def test_403_on_my_own_photo_is_also_the_directory_scope_error(self):
        respx.get(ME_PHOTO_240_URL).mock(return_value=httpx.Response(403, json=GRAPH_ERROR_403))
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(DirectoryScopeMissingError):
                await photos.aget_photo_bytes(client)

    @respx.mock
    async def test_500_propagates_as_a_graph_error(self):
        respx.get(ME_PHOTO_240_URL).mock(return_value=httpx.Response(500, json=GRAPH_ERROR_500))
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(GraphError) as exc_info:
                await photos.aget_photo_bytes(client)

        assert exc_info.value.status_code == 500

    @respx.mock
    async def test_a_bad_size_is_rejected_before_any_request(self):
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(ValueError):
                await photos.aget_photo_bytes(client, size="100x100")

        assert respx.calls.call_count == 0

    @respx.mock
    async def test_a_missing_content_type_header_is_an_empty_string(self):
        """The tool falls back to the metadata MIME when the header is absent."""
        respx.get(ME_PHOTO_240_URL).mock(return_value=httpx.Response(200, content=PHOTO_BYTES))
        async with AsyncGraphClient("tok") as client:
            data, header = await photos.aget_photo_bytes(client)

        assert data == PHOTO_BYTES
        assert header == ""
