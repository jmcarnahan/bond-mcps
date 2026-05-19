"""Tests for the per-provider legacy-file importer."""

import json
import time

from auth.db.importer import LEGACY_DIR_NAME, import_legacy_files


def test_import_github_token(repo, tmp_path):
    cache = tmp_path / ".bond_mcps"
    cache.mkdir()
    (cache / "github.json").write_text(json.dumps({"access_token": "gho_xyz"}))

    result = import_legacy_files(user_key="alice", cache_dir=cache, repo=repo)
    assert result.imported == ["github"]
    assert result.errors == []
    assert repo.get_token("alice", "github")["access_token"] == "gho_xyz"


def test_import_atlassian_with_extras(repo, tmp_path):
    cache = tmp_path / ".bond_mcps"
    cache.mkdir()
    (cache / "atlassian.json").write_text(
        json.dumps(
            {
                "access_token": "atk-1",
                "refresh_token": "rtk-1",
                "expires_at": time.time() + 3600,
                "cloud_id": "atlassian-uuid",
            }
        )
    )

    result = import_legacy_files(user_key="alice", cache_dir=cache, repo=repo)
    assert "atlassian" in result.imported
    got = repo.get_token("alice", "atlassian")
    assert got["access_token"] == "atk-1"
    assert got["refresh_token"] == "rtk-1"
    assert got["cloud_id"] == "atlassian-uuid"


def test_import_microsoft_msal_blob(repo, tmp_path):
    cache = tmp_path / ".bond_mcps"
    cache.mkdir()
    msal_blob = json.dumps(
        {
            "AccessToken": {
                "home_account_id-login.microsoftonline.com-accesstoken-xxx-yyy": {
                    "credential_type": "AccessToken",
                    "secret": "abc",
                }
            },
        }
    )
    (cache / "microsoft.json").write_text(msal_blob)

    result = import_legacy_files(user_key="alice", cache_dir=cache, repo=repo)
    assert "microsoft" in result.imported
    got = repo.get_msal_cache("alice")
    assert got == msal_blob
    # Critically: NOT in provider_tokens
    assert repo.get_token("alice", "microsoft") is None


def test_idempotent_skip_existing_token(repo, tmp_path):
    cache = tmp_path / ".bond_mcps"
    cache.mkdir()
    (cache / "github.json").write_text(json.dumps({"access_token": "v2"}))

    # Already imported
    repo.save_token("alice", "github", {"access_token": "v1"})

    result = import_legacy_files(user_key="alice", cache_dir=cache, repo=repo)
    assert result.imported == []
    assert "github" in result.skipped_existing

    # Original DB row is unchanged
    assert repo.get_token("alice", "github")["access_token"] == "v1"

    # File is NOT archived (preserved for the user to inspect)
    assert (cache / "github.json").exists()


def test_idempotent_skip_existing_msal(repo, tmp_path):
    cache = tmp_path / ".bond_mcps"
    cache.mkdir()
    (cache / "microsoft.json").write_text('{"v": 2}')
    repo.save_msal_cache("alice", '{"v": 1}')

    result = import_legacy_files(user_key="alice", cache_dir=cache, repo=repo)
    assert "microsoft" in result.skipped_existing
    assert repo.get_msal_cache("alice") == '{"v": 1}'


def test_imported_files_are_archived(repo, tmp_path):
    cache = tmp_path / ".bond_mcps"
    cache.mkdir()
    (cache / "github.json").write_text(json.dumps({"access_token": "x"}))

    result = import_legacy_files(user_key="alice", cache_dir=cache, repo=repo)

    assert len(result.archived) == 1
    assert result.archived[0].parent.name == LEGACY_DIR_NAME
    assert result.archived[0].name.startswith("github.json.")
    assert not (cache / "github.json").exists()  # moved, not copied


def test_missing_cache_dir_is_noop(repo, tmp_path):
    cache = tmp_path / "does-not-exist"
    result = import_legacy_files(user_key="alice", cache_dir=cache, repo=repo)
    assert result.imported == []
    assert result.errors == []


def test_malformed_json_is_error_not_crash(repo, tmp_path):
    cache = tmp_path / ".bond_mcps"
    cache.mkdir()
    (cache / "github.json").write_text("{not json")

    result = import_legacy_files(user_key="alice", cache_dir=cache, repo=repo)
    assert result.imported == []
    assert len(result.errors) == 1
    assert result.errors[0][0] == "github"
    # File NOT archived (left for the user to investigate)
    assert (cache / "github.json").exists()


def test_microsoft_blob_must_be_json(repo, tmp_path):
    cache = tmp_path / ".bond_mcps"
    cache.mkdir()
    (cache / "microsoft.json").write_text("not json at all")

    result = import_legacy_files(user_key="alice", cache_dir=cache, repo=repo)
    assert "microsoft" not in result.imported
    assert len(result.errors) == 1
    assert repo.get_msal_cache("alice") is None


def test_multi_provider_import_in_one_pass(repo, tmp_path):
    cache = tmp_path / ".bond_mcps"
    cache.mkdir()
    (cache / "github.json").write_text(json.dumps({"access_token": "gh"}))
    (cache / "atlassian.json").write_text(json.dumps({"access_token": "atl"}))
    (cache / "microsoft.json").write_text('{"AccessToken": {}}')

    result = import_legacy_files(user_key="alice", cache_dir=cache, repo=repo)
    assert set(result.imported) == {"github", "atlassian", "microsoft"}
    assert repo.get_token("alice", "github")["access_token"] == "gh"
    assert repo.get_token("alice", "atlassian")["access_token"] == "atl"
    assert repo.get_msal_cache("alice") == '{"AccessToken": {}}'


def test_msal_blob_round_trip_via_msal_lib(repo, tmp_path):
    """The encrypted MSAL blob can be deserialized back into a SerializableTokenCache.

    This guards against silent corruption of Microsoft auth state.
    """
    import importlib.util

    if importlib.util.find_spec("msal") is None:
        import pytest

        pytest.skip("msal not installed in this environment")
    import msal

    cache_in = msal.SerializableTokenCache()
    # Inject some state so .serialize() returns a non-trivial blob
    cache_in.deserialize(
        '{"AccessToken": {"acct-x-y": {"credential_type": "AccessToken", "secret": "s"}}}'
    )
    blob = cache_in.serialize()

    cache = tmp_path / ".bond_mcps"
    cache.mkdir()
    (cache / "microsoft.json").write_text(blob)

    import_legacy_files(user_key="alice", cache_dir=cache, repo=repo)

    cache_out = msal.SerializableTokenCache()
    cache_out.deserialize(repo.get_msal_cache("alice"))
    # Deserialize round-trip preserves the entries.
    assert cache_out.serialize() == cache_in.serialize()
