"""The client, exercised with a fake transport. No network in this suite."""

import json

import httpx
import pytest

from jira_agent_kit.client import (
    STORY_POINTS_CUSTOM_TYPE,
    SPRINT_CUSTOM_TYPE,
    JiraClient,
    JiraError,
    TokenMissing,
    read_token,
)
from jira_agent_kit.schema import LabelError, Vocabulary, text_adf

SITE = "example.atlassian.net"


def _client(handler):
    return JiraClient(
        site=SITE, email="t@example.com", token="fake-token",
        transport=httpx.MockTransport(handler),
    )


def _vocab():
    return Vocabulary("eng")


# --- issue types: the dynamic-resolution fix ----------------------------------


def test_resolve_issue_type_id_matches_by_name_case_insensitively():
    def handler(request):
        assert str(request.url).endswith("/project/PROJ")
        return httpx.Response(
            200,
            json={"issueTypes": [{"name": "Task", "id": "10005"}, {"name": "Bug", "id": "10006"}]},
        )

    assert _client(handler).resolve_issue_type_id("PROJ", "task") == "10005"


def test_resolve_issue_type_id_is_cached_after_the_first_lookup():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"issueTypes": [{"name": "Task", "id": "10005"}]})

    client = _client(handler)
    client.resolve_issue_type_id("PROJ", "Task")
    client.resolve_issue_type_id("PROJ", "Task")
    assert len(calls) == 1


def test_resolve_issue_type_id_raises_with_the_real_options_on_a_miss():
    def handler(request):
        return httpx.Response(200, json={"issueTypes": [{"name": "Story", "id": "1"}]})

    with pytest.raises(JiraError, match="Story"):
        _client(handler).resolve_issue_type_id("PROJ", "Epic")


# --- custom fields: matched by TYPE, not by a renameable display name --------


def test_find_field_by_custom_type_matches_the_schema_custom_key():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "fields": [
                    {"fieldId": "customfield_10099", "schema": {"custom": STORY_POINTS_CUSTOM_TYPE}},
                    {"fieldId": "customfield_10001", "schema": {"custom": "some.other.type"}},
                ]
            },
        )

    assert _client(handler).find_estimate_field("PROJ", "10005") == "customfield_10099"


def test_find_field_by_custom_type_returns_none_when_absent():
    def handler(request):
        return httpx.Response(200, json={"fields": []})

    assert _client(handler).find_estimate_field("PROJ", "10005") is None
    assert _client(handler).find_sprint_field("PROJ", "10005") is None


def test_find_sprint_field_matches_its_own_custom_type():
    def handler(request):
        return httpx.Response(
            200, json={"fields": [{"fieldId": "customfield_20", "schema": {"custom": SPRINT_CUSTOM_TYPE}}]}
        )

    assert _client(handler).find_sprint_field("PROJ", "10005") == "customfield_20"


# --- create_issue --------------------------------------------------------------


def test_create_issue_sends_every_field_the_request_exists_for():
    seen = {}

    def handler(request):
        seen["json"] = json.loads(request.content)
        return httpx.Response(201, json={"key": "PROJ-42"})

    body = text_adf("Because it is missing.")
    key = _client(handler).create_issue(
        project_key="PROJ", issue_type_id="10005", summary="Add the thing",
        description_adf=body, labels=["eng-backend", "eng-feature", "eng-by-agent"],
        vocabulary=_vocab(),
    )
    assert key == "PROJ-42"
    fields = seen["json"]["fields"]
    assert fields["project"] == {"key": "PROJ"}
    assert fields["summary"] == "Add the thing"
    assert fields["description"] == body
    assert fields["labels"] == ["eng-backend", "eng-by-agent", "eng-feature"]


def test_create_issue_rejects_a_bad_label_before_any_request():
    def handler(request):  # pragma: no cover - must never run
        raise AssertionError("a request was sent despite an invalid label")

    with pytest.raises(LabelError):
        _client(handler).create_issue(
            project_key="PROJ", issue_type_id="10005", summary="s",
            description_adf=text_adf("d"),
            labels=["eng-fronted", "eng-feature", "eng-by-agent"],
            vocabulary=_vocab(),
        )


def test_parent_key_is_sent_as_the_parent_field():
    seen = {}

    def handler(request):
        seen["json"] = json.loads(request.content)
        return httpx.Response(201, json={"key": "PROJ-43"})

    _client(handler).create_issue(
        project_key="PROJ", issue_type_id="10005", summary="child",
        description_adf=text_adf("d"), labels=["eng-backend", "eng-feature", "eng-by-agent"],
        vocabulary=_vocab(), parent_key="PROJ-9",
    )
    assert seen["json"]["fields"]["parent"] == {"key": "PROJ-9"}


def test_extra_fields_are_merged_in():
    seen = {}

    def handler(request):
        seen["json"] = json.loads(request.content)
        return httpx.Response(201, json={"key": "PROJ-44"})

    _client(handler).create_issue(
        project_key="PROJ", issue_type_id="10005", summary="s",
        description_adf=text_adf("d"), labels=["eng-backend", "eng-feature", "eng-by-agent"],
        vocabulary=_vocab(), extra_fields={"customfield_1": 3, "priority": {"name": "High"}},
    )
    assert seen["json"]["fields"]["customfield_1"] == 3
    assert seen["json"]["fields"]["priority"] == {"name": "High"}


# --- comments and labels --------------------------------------------------------


def test_add_comment_posts_to_the_comment_endpoint_with_a_body_key():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["json"] = json.loads(request.content)
        return httpx.Response(201, json={})

    body = text_adf("hello")
    _client(handler).add_comment("PROJ-42", body)
    assert seen["url"].endswith("/issue/PROJ-42/comment")
    assert seen["json"] == {"body": body}


def test_list_comments_returns_newest_first_and_respects_the_limit():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "comments": [
                    {"body": {"content": [{"content": [{"type": "text", "text": "first"}]}]}},
                    {"body": {"content": [{"content": [{"type": "text", "text": "second"}]}]}},
                ]
            },
        )

    got = _client(handler).list_comments("PROJ-10", limit=3)
    assert got == ["first", "second"]
    assert "maxResults=3" in seen["url"]
    assert "orderBy=-created" in seen["url"]


def test_add_labels_uses_the_update_verb_so_existing_labels_survive():
    seen = {}

    def handler(request):
        seen["json"] = json.loads(request.content)
        return httpx.Response(204)

    _client(handler).add_labels("PROJ-42", ["eng-hurts-scale"], _vocab())
    assert seen["json"] == {"update": {"labels": [{"add": "eng-hurts-scale"}]}}


def test_add_labels_rejects_an_unknown_label_before_any_request():
    def handler(request):  # pragma: no cover
        raise AssertionError("a request was sent despite an invalid label")

    with pytest.raises(LabelError, match="eng-fronted"):
        _client(handler).add_labels("PROJ-42", ["eng-fronted"], _vocab())


# --- issues, transitions, deletion -----------------------------------------------


def test_get_issue_reads_the_issue_endpoint():
    def handler(request):
        assert str(request.url).endswith("/issue/PROJ-1")
        return httpx.Response(200, json={"fields": {"summary": "s"}})

    assert _client(handler).get_issue("PROJ-1")["fields"]["summary"] == "s"


def test_delete_issue_sends_a_delete_to_the_issue_endpoint():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(204)

    _client(handler).delete_issue("PROJ-99")
    assert seen["method"] == "DELETE"
    assert seen["url"].endswith("/issue/PROJ-99")


def test_list_transitions_maps_name_to_id():
    def handler(request):
        return httpx.Response(
            200, json={"transitions": [{"id": "31", "name": "Done"}, {"id": "11", "name": "To Do"}]}
        )

    assert _client(handler).list_transitions("PROJ-1") == {"Done": "31", "To Do": "11"}


def test_transition_posts_the_transition_id():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["json"] = json.loads(request.content)
        return httpx.Response(204)

    _client(handler).transition("PROJ-1", "31")
    assert seen["url"].endswith("/issue/PROJ-1/transitions")
    assert seen["json"] == {"transition": {"id": "31"}}


# --- Agile API: boards and sprints ---------------------------------------------


def test_find_board_id_returns_the_first_boards_id():
    def handler(request):
        assert "/rest/agile/1.0/board" in str(request.url)
        return httpx.Response(200, json={"values": [{"id": 42, "name": "Board"}]})

    assert _client(handler).find_board_id("PROJ") == 42


def test_find_board_id_returns_none_when_the_project_has_no_board():
    def handler(request):
        return httpx.Response(200, json={"values": []})

    assert _client(handler).find_board_id("PROJ") is None


def test_list_sprints_returns_id_name_and_state():
    def handler(request):
        assert "/rest/agile/1.0/board/42/sprint" in str(request.url)
        return httpx.Response(
            200,
            json={"values": [{"id": 1, "name": "Sprint 1", "state": "future"}]},
        )

    assert _client(handler).list_sprints(42) == [{"id": 1, "name": "Sprint 1", "state": "future"}]


def test_the_sprint_field_is_sent_as_a_bare_int_not_an_array():
    # Verified live against a real Jira site: an array (`[1]`), a string
    # ("1"), and an array of strings were all rejected with "Specify a
    # valid value for Sprint". Only a bare int is accepted.
    seen = {}

    def handler(request):
        seen["json"] = json.loads(request.content)
        return httpx.Response(201, json={"key": "PROJ-1"})

    _client(handler).create_issue(
        project_key="PROJ", issue_type_id="10005", summary="s",
        description_adf=text_adf("d"), labels=["eng-backend", "eng-feature", "eng-by-agent"],
        vocabulary=_vocab(), extra_fields={"customfield_20": 1},
    )
    assert seen["json"]["fields"]["customfield_20"] == 1
    assert not isinstance(seen["json"]["fields"]["customfield_20"], list)


# --- filters and dashboards -----------------------------------------------------


def test_create_filter_posts_name_jql_and_description():
    seen = {}

    def handler(request):
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "100"})

    fid = _client(handler).create_filter("F", "project = PROJ", "desc")
    assert fid == "100"
    assert seen["json"] == {"name": "F", "jql": "project = PROJ", "description": "desc"}


def test_create_dashboard_posts_name_and_description():
    seen = {}

    def handler(request):
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "200"})

    did = _client(handler).create_dashboard("D", "desc")
    assert did == "200"
    assert seen["json"]["name"] == "D"
    assert seen["json"]["sharePermissions"] == []


def test_add_dashboard_gadget_posts_uri_color_and_position():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 900})

    gid = _client(handler).add_dashboard_gadget(
        dashboard_id="200", uri="rest/.../filter-results-gadget.xml",
        color="red", row=0, column=1, title="T",
    )
    assert gid == 900
    assert seen["url"].endswith("/dashboard/200/gadget")
    assert seen["json"]["position"] == {"row": 0, "column": 1}


def test_configure_gadget_puts_the_raw_config_dict():
    # Verified live before this was written: the write body is the config
    # dict directly, not wrapped in {"value": ...} the way the READ
    # response is.
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["json"] = json.loads(request.content)
        return httpx.Response(201)

    _client(handler).configure_gadget(dashboard_id="200", gadget_id=900, config={"filterId": "100"})
    assert seen["method"] == "PUT"
    assert seen["url"].endswith("/dashboard/200/items/900/properties/config")
    assert seen["json"] == {"filterId": "100"}


# --- identity and errors --------------------------------------------------------


def test_myself_reads_the_myself_endpoint():
    def handler(request):
        assert str(request.url).endswith("/myself")
        return httpx.Response(200, json={"emailAddress": "t@example.com", "active": True})

    got = _client(handler).myself()
    assert got["emailAddress"] == "t@example.com"


def test_an_error_response_carries_the_status_and_the_body():
    def handler(request):
        return httpx.Response(400, json={"errorMessages": ["Field 'labels' invalid"]})

    with pytest.raises(JiraError) as caught:
        _client(handler).add_comment("PROJ-42", text_adf("hi"))
    assert "400" in str(caught.value)
    assert "labels" in str(caught.value)


def test_the_token_never_leaks_in_any_encoding():
    """Basic auth base64-encodes email:token at construction, so the raw
    token never exists in plaintext anywhere. Asserting only the plaintext's
    absence would be vacuously true even if the client printed the whole
    Authorization header -- this checks the encoded form too.
    """
    import base64

    token = "fake-token"
    encoded = base64.b64encode(f"t@example.com:{token}".encode()).decode()

    def handler(request):
        return httpx.Response(401, json={"errorMessages": ["Unauthorized"]})

    with pytest.raises(JiraError) as caught:
        _client(handler).get_issue("PROJ-42")
    message = str(caught.value)
    assert token not in message
    assert encoded not in message
    assert "Authorization" not in message


def test_a_malformed_json_response_becomes_a_jira_error():
    def handler(request):
        return httpx.Response(200, content=b"<html>gateway timeout</html>")

    with pytest.raises(JiraError) as caught:
        _client(handler).get_issue("PROJ-42")
    assert "could not be read" in str(caught.value)


def test_a_network_failure_becomes_a_jira_error():
    def handler(request):
        raise httpx.ConnectError("nodename nor servname provided")

    with pytest.raises(JiraError) as caught:
        _client(handler).get_issue("PROJ-42")
    assert "could not be sent" in str(caught.value)


# --- read_token ------------------------------------------------------------------


def test_read_token_finds_the_keychain_item(monkeypatch):
    def fake_run(cmd, **kwargs):
        assert "-s" in cmd and "-a" in cmd

        class Done:
            returncode = 0
            stdout = "a-real-token\n"
            stderr = ""

        return Done()

    monkeypatch.setattr("jira_agent_kit.client.subprocess.run", fake_run)
    assert read_token(service="svc", account="t@example.com") == "a-real-token"


def test_a_missing_keychain_item_raises_token_missing(monkeypatch):
    def fake_run(*args, **kwargs):
        class Done:
            returncode = 44
            stdout = ""
            stderr = "not found"

        return Done()

    monkeypatch.setattr("jira_agent_kit.client.subprocess.run", fake_run)
    with pytest.raises(TokenMissing) as caught:
        read_token(service="svc", account="t@example.com")
    assert "security add-generic-password" in str(caught.value)


def test_an_empty_stdout_with_a_zero_return_code_is_also_token_missing(monkeypatch):
    # Two separate failure modes ORed together; both must be covered
    # independently or one half is untested.
    def fake_run(*args, **kwargs):
        class Done:
            returncode = 0
            stdout = "   \n"
            stderr = ""

        return Done()

    monkeypatch.setattr("jira_agent_kit.client.subprocess.run", fake_run)
    with pytest.raises(TokenMissing):
        read_token(service="svc", account="t@example.com")


def test_a_machine_without_the_security_binary_gets_token_missing(monkeypatch):
    def no_binary(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "security")

    monkeypatch.setattr("jira_agent_kit.client.subprocess.run", no_binary)
    with pytest.raises(TokenMissing):
        read_token(service="svc", account="t@example.com")


def test_the_missing_token_message_gives_a_command_that_can_actually_work(monkeypatch):
    # `security add-generic-password -w` truncates at 128 characters when
    # reading from its own prompt or from stdin, and an Atlassian API token
    # is about 192 -- the interactive form fails silently. The message must
    # show the argument form.
    def fake_run(*args, **kwargs):
        class Done:
            returncode = 44
            stdout = ""
            stderr = "not found"

        return Done()

    monkeypatch.setattr("jira_agent_kit.client.subprocess.run", fake_run)
    with pytest.raises(TokenMissing) as caught:
        read_token(service="svc", account="t@example.com")
    message = str(caught.value)
    assert '-w "$(pbpaste' in message
