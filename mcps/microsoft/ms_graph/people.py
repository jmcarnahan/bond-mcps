"""Directory lookups: who is in the organisation.

/users with $search, plus a single-user lookup, both under
User.ReadBasic.All. Everything else in this package reads the signed-in user's
own data; this is the only place the server looks at other users, and it
returns only the address-book properties a typeahead needs.
"""

from urllib.parse import quote

from .graph_client import AsyncGraphClient, GraphClient, GraphError

DIRECTORY_SELECT = "id,displayName,mail,userPrincipalName,jobTitle"
MAX_DIRECTORY_TOP = 50
# $search on directory objects needs this header and $count=true, or Graph
# answers 400.
_ADVANCED_QUERY_HEADERS = {"ConsistencyLevel": "eventual"}


class DirectoryScopeMissingError(Exception):
    """The connection lacks User.ReadBasic.All, so no directory search can succeed."""


def _search_clause(query: str) -> str:
    """Escape a value for a $search clause. Graph wants \\ and " backslash-escaped
    inside the quoted clause and documents that an & fails outright, so it is
    dropped."""
    return query.replace("\\", "\\\\").replace('"', '\\"').replace("&", "")


def _search_path(query: str, top: int) -> str:
    """Build the /users search URL by hand (Graph rejects + for space)."""
    top = max(1, min(int(top), MAX_DIRECTORY_TOP))
    clause = _search_clause(query)
    search = quote(f'"displayName:{clause}" OR "mail:{clause}"', safe="")
    return (
        f"/users?$search={search}&$select={quote(DIRECTORY_SELECT)}"
        f"&$top={top}&$count=true&$orderby=displayName"
    )


def _raise_directory_scope_missing(e: GraphError) -> None:
    """A 403 on /users is the missing directory scope; anything else propagates."""
    if e.status_code == 403:
        raise DirectoryScopeMissingError() from e
    raise e


def _user_path(user: str) -> str:
    """/users/{user} with the id or UPN quoted as ONE path segment.

    Guest UPNs carry '#EXT#' and a hostile value could carry '/', so the
    segment is quoted with only '@' left bare.
    """
    return f"/users/{quote(user, safe='@')}"


def get_user(client: GraphClient, user: str) -> dict:
    """Basic directory profile of one user (sync).

    403 -> DirectoryScopeMissingError; a 404 (unknown user) propagates as
    GraphError so a caller can tell "no such user" from "no permission".
    """
    try:
        return client.get(f"{_user_path(user)}?$select={DIRECTORY_SELECT}")
    except GraphError as e:
        _raise_directory_scope_missing(e)
        raise  # unreachable: _raise_directory_scope_missing always raises


async def aget_user(client: AsyncGraphClient, user: str) -> dict:
    """Basic directory profile of one user (async). See get_user."""
    try:
        return await client.get(f"{_user_path(user)}?$select={DIRECTORY_SELECT}")
    except GraphError as e:
        _raise_directory_scope_missing(e)
        raise  # unreachable: _raise_directory_scope_missing always raises


def search_users(client: GraphClient, query: str, top: int = 10) -> list[dict]:
    """Search the directory by display-name token or mail prefix.

    A query with nothing searchable left once it is escaped (an "&" alone,
    say) returns an empty list without a request: Graph would answer 400 to
    an empty clause, and a client would read that as "retry".
    """
    if not _search_clause(query).strip():
        return []
    try:
        data = client.get(_search_path(query, top), headers=_ADVANCED_QUERY_HEADERS)
    except GraphError as e:
        _raise_directory_scope_missing(e)
    return data.get("value", [])


async def asearch_users(client: AsyncGraphClient, query: str, top: int = 10) -> list[dict]:
    """Search the directory by display-name token or mail prefix (async)."""
    if not _search_clause(query).strip():
        return []
    try:
        data = await client.get(_search_path(query, top), headers=_ADVANCED_QUERY_HEADERS)
    except GraphError as e:
        _raise_directory_scope_missing(e)
    return data.get("value", [])
