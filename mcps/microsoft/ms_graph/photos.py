"""Profile photos: the signed-in user's, or another directory user's.

Graph facts this module is built on (verified live 2026-09-07):

- ``GET {base}/photo`` returns ``{"id", "width", "height",
  "@odata.mediaContentType"}`` on 200, and **404 ``ImageNotFound``** when the
  user has no photo set. No photo is a normal result, so the helpers answer
  ``None`` rather than raising.
- ``GET {base}/photos/{size}/$value`` serves every fixed size even when the
  stored original is smaller — Graph upscales — and the served variant keeps
  the original's MIME type (jpeg stays jpeg, png stays png). There is
  therefore no size-fallback logic to write.
- ``GET {base}/photo/$value`` serves the original, whatever its dimensions.
- A size outside Graph's fixed set is a **400 ``ErrorInvalidImageId``**, so
  sizes are validated here before any request goes out.
- The signed-in user's own photo needs only ``User.Read``; another user's
  needs ``User.ReadBasic.All``. A tenant policy can still 403 ``/me/photo``,
  so a 403 anywhere is surfaced as ``DirectoryScopeMissingError`` and the tool
  collapses it to one error.
"""

from .graph_client import AsyncGraphClient, GraphClient, GraphError
from .people import DirectoryScopeMissingError, _user_path

PHOTO_SIZES = (
    "48x48",
    "64x64",
    "96x96",
    "120x120",
    "240x240",
    "360x360",
    "432x432",
    "504x504",
    "648x648",
)
DEFAULT_PHOTO_SIZE = "240x240"
ORIGINAL = "original"


def photo_base(user: str = "") -> str:
    """'/me' for the signed-in user, else the quoted /users/{user} path."""
    return _user_path(user) if user else "/me"


def check_photo_size(size: str) -> str:
    """Normalise a requested size, or raise ValueError naming the allowed set.

    An empty value becomes the default; case and surrounding space are
    ignored, so "ORIGINAL" is accepted as "original". Anything outside
    PHOTO_SIZES (a made-up "100x100", a word like "big") is rejected here
    rather than at Graph, which answers 400.
    """
    size = (size or "").strip().lower()
    if not size:
        return DEFAULT_PHOTO_SIZE
    if size == ORIGINAL or size in PHOTO_SIZES:
        return size
    raise ValueError(
        f"photo_size must be one of: {', '.join(PHOTO_SIZES)}, or original; got {size!r}"
    )


def _value_path(user: str, size: str) -> str:
    """The $value URL for one size ('original' has its own endpoint)."""
    base = photo_base(user)
    return f"{base}/photo/$value" if size == ORIGINAL else f"{base}/photos/{size}/$value"


def _map_photo_error(e: GraphError) -> None:
    """403 becomes the directory-scope error; anything else re-raises.

    404 never reaches here: the callers read it as "no photo set".
    """
    if e.status_code == 403:
        raise DirectoryScopeMissingError() from e
    raise e


def get_photo_metadata(client: GraphClient, user: str = "") -> dict | None:
    """Photo metadata (width, height, @odata.mediaContentType), or None when
    no photo is set (sync)."""
    try:
        return client.get(f"{photo_base(user)}/photo")
    except GraphError as e:
        if e.status_code == 404:
            return None
        _map_photo_error(e)
        raise  # unreachable: _map_photo_error always raises


async def aget_photo_metadata(client: AsyncGraphClient, user: str = "") -> dict | None:
    """Photo metadata, or None when no photo is set (async). See
    get_photo_metadata."""
    try:
        return await client.get(f"{photo_base(user)}/photo")
    except GraphError as e:
        if e.status_code == 404:
            return None
        _map_photo_error(e)
        raise  # unreachable: _map_photo_error always raises


def get_photo_bytes(
    client: GraphClient, user: str = "", size: str = ""
) -> tuple[bytes, str] | None:
    """(bytes, Content-Type header) of the photo at `size`, or None when no
    photo is set (sync). `size` is normalised by check_photo_size, which
    raises ValueError before any request for a size Graph does not serve."""
    size = check_photo_size(size)
    try:
        return client.get_bytes_with_type(_value_path(user, size))
    except GraphError as e:
        if e.status_code == 404:
            return None
        _map_photo_error(e)
        raise  # unreachable: _map_photo_error always raises


async def aget_photo_bytes(
    client: AsyncGraphClient, user: str = "", size: str = ""
) -> tuple[bytes, str] | None:
    """(bytes, Content-Type header) of the photo at `size`, or None when no
    photo is set (async). See get_photo_bytes."""
    size = check_photo_size(size)
    try:
        return await client.get_bytes_with_type(_value_path(user, size))
    except GraphError as e:
        if e.status_code == 404:
            return None
        _map_photo_error(e)
        raise  # unreachable: _map_photo_error always raises
