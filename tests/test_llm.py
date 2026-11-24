"""Retry / fallback behaviour of triage.llm.generate_json, with the Gemini API mocked out."""

from unittest.mock import patch

import pytest
from google.genai import errors

from triage import llm
from triage.schemas import RequirementList

GOOD_JSON = '{"requirements": []}'


def api_error(code: int, message: str) -> errors.APIError:
    return errors.APIError(code, {"error": {"code": code, "message": message, "status": "X"}})


def run(replies, models="primary,fallback"):
    """Call generate_json while _call returns/raises the given replies in order."""
    calls = []

    def fake_call(client, model, prompt, schema, system, set_thinking=True):
        calls.append(model)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    primary, fallback = models.split(",", 1)
    env = {"GEMINI_MODEL": primary, "GEMINI_FALLBACK_MODEL": fallback}
    with patch.dict("os.environ", env), patch.object(llm, "_call", side_effect=fake_call), \
         patch.object(llm.time, "sleep"):
        result = llm.generate_json("key", "prompt", RequirementList, "system")
    return result, calls


def test_models_to_try_reads_comma_separated_fallbacks_without_duplicates():
    with patch.dict("os.environ", {"GEMINI_MODEL": "a", "GEMINI_FALLBACK_MODEL": "b, a ,c"}):
        assert llm.models_to_try() == ["a", "b", "c"]


def test_busy_model_is_retried_then_succeeds():
    (_, model), calls = run([api_error(503, "high demand"), GOOD_JSON])
    assert model == "primary" and calls == ["primary", "primary"]


def test_daily_quota_skips_straight_to_fallback():
    quota = api_error(429, "quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier")
    (_, model), calls = run([quota, GOOD_JSON])
    assert model == "fallback" and calls == ["primary", "fallback"]  # no pointless retries


def test_invalid_json_is_retried_once():
    (_, model), calls = run(['{"requirements": "oops"}', GOOD_JSON])
    assert model == "primary" and calls == ["primary", "primary"]


def test_all_models_failing_gives_clear_message():
    quota = api_error(429, "PerDay quota")
    with pytest.raises(llm.LLMError, match="20 requests per model per day"):
        run([quota, quota])


def test_bad_api_key_stops_immediately():
    with pytest.raises(llm.LLMError, match="API key"):
        run([api_error(400, "API key not valid")])


def test_model_rejecting_thinking_level_is_retried_with_default():
    (_, model), calls = run([api_error(400, "Thinking level MINIMAL is not supported for this model."), GOOD_JSON])
    assert model == "primary" and calls == ["primary", "primary"]
