"""Tests for the shared options_parser utility."""

from auth.options_parser import opt_bool, opt_int, opt_str, parse_options


class TestParseOptions:
    """Test parse_options function."""

    def test_empty_string_returns_empty_dict(self):
        result, err = parse_options("")
        assert result == {}
        assert err is None

    def test_whitespace_only_returns_empty_dict(self):
        result, err = parse_options("   ")
        assert result == {}
        assert err is None

    def test_valid_json_object(self):
        result, err = parse_options('{"key": "value", "count": 5}')
        assert result == {"key": "value", "count": 5}
        assert err is None

    def test_valid_json_with_boolean(self):
        result, err = parse_options('{"draft": true, "per_page": 30}')
        assert result == {"draft": True, "per_page": 30}
        assert err is None

    def test_valid_json_nested(self):
        result, err = parse_options('{"labels": "bug,feature", "state": "open"}')
        assert result == {"labels": "bug,feature", "state": "open"}
        assert err is None

    def test_invalid_json_returns_error(self):
        result, err = parse_options("not json")
        assert result == {}
        assert err is not None
        assert "valid JSON" in err

    def test_json_array_returns_error(self):
        result, err = parse_options("[1, 2, 3]")
        assert result == {}
        assert err is not None
        assert "JSON object" in err

    def test_json_string_returns_error(self):
        result, err = parse_options('"just a string"')
        assert result == {}
        assert err is not None
        assert "JSON object" in err

    def test_json_number_returns_error(self):
        result, err = parse_options("42")
        assert result == {}
        assert err is not None
        assert "JSON object" in err

    def test_none_like_empty(self):
        result, err = parse_options("")
        assert result == {}
        assert err is None

    def test_none_input_treated_as_empty(self):
        # Type hint says str but callers could pass None defensively
        result, err = parse_options(None)  # type: ignore[arg-type]
        assert result == {}
        assert err is None

    def test_empty_object(self):
        result, err = parse_options("{}")
        assert result == {}
        assert err is None


class TestOptInt:
    """Test opt_int coercion helper."""

    def test_int_passthrough(self):
        assert opt_int(30, 10) == 30

    def test_string_number_coerced(self):
        assert opt_int("50", 10) == 50

    def test_float_truncated(self):
        assert opt_int(30.7, 10) == 30

    def test_none_returns_default(self):
        assert opt_int(None, 10) == 10

    def test_invalid_string_returns_default(self):
        assert opt_int("abc", 10) == 10

    def test_empty_string_returns_default(self):
        assert opt_int("", 10) == 10


class TestOptBool:
    """Test opt_bool coercion helper."""

    def test_true_passthrough(self):
        assert opt_bool(True, False) is True

    def test_false_passthrough(self):
        assert opt_bool(False, True) is False

    def test_string_true(self):
        assert opt_bool("true", False) is True

    def test_string_True_case_insensitive(self):
        assert opt_bool("True", False) is True

    def test_string_1(self):
        assert opt_bool("1", False) is True

    def test_string_false(self):
        assert opt_bool("false", True) is False

    def test_string_no(self):
        assert opt_bool("no", True) is False

    def test_none_returns_default(self):
        assert opt_bool(None, True) is True

    def test_int_returns_default(self):
        assert opt_bool(1, False) is False


class TestOptStr:
    """Test opt_str coercion helper."""

    def test_string_passthrough(self):
        assert opt_str("abc") == "abc"

    def test_string_is_stripped(self):
        assert opt_str("  abc  ") == "abc"

    def test_none_returns_none(self):
        assert opt_str(None) is None

    def test_empty_string_returns_none(self):
        assert opt_str("") is None

    def test_whitespace_only_returns_none(self):
        assert opt_str("   ") is None

    def test_non_string_coerced_to_string(self):
        # A numeric JSON value must not crash downstream str-only code paths.
        assert opt_str(123) == "123"

    def test_zero_coerced_to_string(self):
        # 0 is falsy but a legitimate value; it must coerce, not drop to None.
        assert opt_str(0) == "0"
