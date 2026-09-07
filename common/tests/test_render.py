"""Rules of the compact renderer — the contract every tool docstring cites."""

from bond_common.render import render_compact


class TestTables:
    def test_flat_rows_render_as_pipe_csv(self):
        payload = {
            "messages": [
                {"id": "1", "subject": "Hi", "unread": True},
                {"id": "2", "subject": "Bye", "unread": False},
            ]
        }
        assert render_compact(payload) == ("id|subject|unread\n1|Hi|true\n2|Bye|false")

    def test_header_is_the_union_of_ragged_row_keys_in_first_seen_order(self):
        payload = {
            "rows": [
                {"id": "1", "name": "a"},
                {"id": "2", "size": 9},
                {"name": "c", "id": "3"},
            ]
        }
        assert render_compact(payload) == "id|name|size\n1|a|\n2||9\n3|c|"

    def test_none_cells_render_empty(self):
        payload = {"rows": [{"id": "1", "topic": None}]}
        assert render_compact(payload) == "id|topic\n1|"

    def test_booleans_render_lowercase(self):
        payload = {"rows": [{"read": True, "flagged": False}]}
        assert render_compact(payload) == "read|flagged\ntrue|false"

    def test_embedded_pipe_is_quoted(self):
        payload = {"rows": [{"subject": "budget | Q3"}]}
        assert render_compact(payload) == 'subject\n"budget | Q3"'

    def test_embedded_newline_is_quoted(self):
        payload = {"rows": [{"body": "line one\nline two"}]}
        assert render_compact(payload) == 'body\n"line one\nline two"'

    def test_scalar_siblings_become_trailing_lines(self):
        payload = {
            "messages": [{"id": "1"}],
            "count": 1,
            "next_cursor": "abc",
        }
        assert render_compact(payload) == "id\n1\ncount: 1\nnext_cursor: abc"

    def test_empty_and_null_siblings_are_dropped_but_zero_survives(self):
        payload = {
            "messages": [{"id": "1"}],
            "next_cursor": None,
            "notice": "",
            "count": 0,
        }
        assert render_compact(payload) == "id\n1\ncount: 0"

    def test_empty_list_renders_as_none_marker_with_trailing_scalars(self):
        payload = {"people": [], "query": "bob"}
        assert render_compact(payload) == "people: (none)\nquery: bob"

    def test_two_list_keys_fall_back_to_json(self):
        payload = {"a": [{"x": 1}], "b": [{"y": 2}]}
        assert render_compact(payload) == '{"a":[{"x":1}],"b":[{"y":2}]}'

    def test_rows_with_nested_values_fall_back_to_json(self):
        payload = {"rows": [{"id": "1", "meta": {"k": "v"}}]}
        assert render_compact(payload) == '{"rows":[{"id":"1","meta":{"k":"v"}}]}'

    def test_nested_sibling_of_a_table_falls_back_to_json(self):
        payload = {"rows": [{"id": "1"}], "paging": {"cursor": "c"}}
        assert render_compact(payload) == '{"rows":[{"id":"1"}],"paging":{"cursor":"c"}}'


class TestRecordsAndErrors:
    def test_all_scalar_dict_renders_as_key_value_lines(self):
        payload = {"connected": True, "account": "a@b.com", "expires": None}
        assert render_compact(payload) == "connected: true\naccount: a@b.com\nexpires: "

    def test_error_key_is_hoisted_to_the_first_line(self):
        payload = {"connect_url": "https://x/connect", "error": "not_connected"}
        assert render_compact(payload) == "error: not_connected\nconnect_url: https://x/connect"

    def test_error_envelope_keeps_non_scalar_siblings_as_compact_json(self):
        payload = {"updated": 0, "failed": ["a", "b"], "error": "graph_error"}
        assert render_compact(payload) == 'error: graph_error\nupdated: 0\nfailed: ["a","b"]'

    def test_nested_payload_falls_back_to_compact_json(self):
        payload = {"event": {"subject": "Sync", "attendees": ["a", "b"]}}
        assert render_compact(payload) == '{"event":{"subject":"Sync","attendees":["a","b"]}}'

    def test_empty_payload_renders_as_empty_json_object(self):
        assert render_compact({}) == "{}"

    def test_non_serializable_values_use_str_in_the_json_fallback(self):
        payload = {"blob": {"data": object()}}
        assert render_compact(payload).startswith('{"blob":{"data":"<object object at')


class TestOverrides:
    def test_override_wins_over_every_other_rule(self):
        payload = {"rows": [{"id": "1"}]}
        overrides = {"list_files": lambda p: f"custom:{len(p['rows'])}"}
        assert render_compact(payload, "list_files", overrides) == "custom:1"

    def test_override_for_another_tool_is_ignored(self):
        payload = {"rows": [{"id": "1"}]}
        overrides = {"list_files": lambda p: "custom"}
        assert render_compact(payload, "list_emails", overrides) == "id\n1"

    def test_override_applies_to_error_payloads_too(self):
        overrides = {"t": lambda p: "handled"}
        assert render_compact({"error": "boom"}, "t", overrides) == "handled"
