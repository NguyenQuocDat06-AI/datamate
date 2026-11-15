import httpx
import pytest

from bizreason.config import Settings
from bizreason.llm import (TrammayClient, ModelError, InvalidJSONError,
                          IncompleteCandidateError, EmptyCandidateError, parse_json_object)


@pytest.mark.parametrize('response,error,code', [
    ({'choices':[{'finish_reason':'length','message':{'content':'{"partial":'}}]},
     IncompleteCandidateError,'output_incomplete'),
    ({'choices':[{'finish_reason':'stop','message':{'content':'not JSON'}}]},
     InvalidJSONError,'invalid_json'),
    ({'choices':[{'finish_reason':'stop','message':{'content':''}}]},
     EmptyCandidateError,'empty_candidate'),
])
def test_failure_category_does_not_expose_response(response,error,code):
    client=TrammayClient(Settings(api_key='unit-test-secret'), transport=httpx.MockTransport(
        lambda request: httpx.Response(200,json=response)))
    try:
        with pytest.raises(error) as caught:
            client.generate_json('', '', timeout=1)
        assert isinstance(caught.value, ModelError)
        assert caught.value.code == code
        assert 'unit-test-secret' not in str(caught.value)
    finally:
        client.close()


@pytest.mark.parametrize('text,expected', [
    ('{"items":[{"v":3]}', {'items':[{'v':3}]}),
    ('{"a":{"b":[1,2}}', {'a':{'b':[1,2]}}),
    ('{"a":[{"b":{"c":false]}', {'a':[{'b':{'c':False}}]}),
    ('{"items":[{"v":"a]b}c"],"ok":true}', {'items':[{'v':'a]b}c'}], 'ok':True}),
])
def test_missing_inner_close_is_repaired_without_changing_values(text, expected):
    assert parse_json_object(text) == expected


@pytest.mark.parametrize('text', ['{"items":[{"v":', '{"items":[{"v":3]', '{"items":[{"v":"unfinished]}'])
def test_repair_cannot_invent_a_value_string_or_root_at_eof(text):
    with pytest.raises(InvalidJSONError):
        parse_json_object(text)
