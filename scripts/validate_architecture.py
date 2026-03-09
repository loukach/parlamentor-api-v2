"""Phase 0, Task 7: Technical validation tests.

Validates 5 key architectural assumptions before Phase 1:
  A. Structured output + adaptive thinking compatibility
  B. Prompt caching basics
  C. Langfuse SDK v3 integration
  D. Agent extraction pattern (multi-turn then extract)
  E. Strict tool schemas

Requires: ANTHROPIC_API_KEY and LANGFUSE_* in .env
"""

import json
import os
import time
import traceback

from dotenv import load_dotenv

load_dotenv()

import anthropic

client = anthropic.Anthropic()

PASS = "[PASS]"
FAIL = "[FAIL]"
results: list[tuple[str, str, str]] = []

# Reusable JSON schema for structured output
FACTS_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "facts": {
                "type": "array",
                "items": {"type": "string"},
            }
        },
        "required": ["facts"],
        "additionalProperties": False,
    },
}

PARTIES_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "parties": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "ideology": {"type": "string"},
                    },
                    "required": ["name", "ideology"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["parties"],
        "additionalProperties": False,
    },
}


def report(test_id: str, name: str, passed: bool, details: str = ""):
    status = PASS if passed else FAIL
    results.append((test_id, name, status))
    print(f"\n{'='*60}")
    print(f"  {status} Test {test_id}: {name}")
    if details:
        print(f"  {details}")
    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# Test A: Structured output + adaptive thinking compatibility
# ---------------------------------------------------------------------------
def test_a():
    """Can we use output_config.format AND thinking in the same API call?"""
    try:
        response = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=4000,
            thinking={
                "type": "enabled",
                "budget_tokens": 2000,
            },
            messages=[
                {"role": "user", "content": "List 2 facts about Portugal's parliament."}
            ],
            output_config={"format": FACTS_SCHEMA},
        )

        has_thinking = any(b.type == "thinking" for b in response.content)

        text_block = next(b for b in response.content if b.type == "text")
        parsed = json.loads(text_block.text)
        has_facts = isinstance(parsed.get("facts"), list) and len(parsed["facts"]) >= 2

        report(
            "A",
            "Structured output + thinking",
            has_thinking and has_facts,
            f"thinking={has_thinking}, facts={len(parsed.get('facts', []))}",
        )
    except Exception as e:
        report("A", "Structured output + thinking", False, str(e))


# ---------------------------------------------------------------------------
# Test B: Prompt caching basics
# ---------------------------------------------------------------------------
def test_b():
    """Does prompt caching work? Send same system prompt twice, check cache_read."""
    try:
        # System prompt must be >= 1024 tokens to be cacheable
        system_prompt = [
            {
                "type": "text",
                "text": ("You are an expert on Portuguese parliamentary procedures. " * 200),
                "cache_control": {"type": "ephemeral"},
            }
        ]

        # First call: creates cache
        r1 = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=50,
            system=system_prompt,
            messages=[{"role": "user", "content": "Say 'hello'."}],
        )
        cache_create_1 = r1.usage.cache_creation_input_tokens or 0

        # Second call: should read from cache
        r2 = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=50,
            system=system_prompt,
            messages=[{"role": "user", "content": "Say 'world'."}],
        )
        cache_read_2 = r2.usage.cache_read_input_tokens or 0

        report(
            "B",
            "Prompt caching",
            cache_create_1 > 0 and cache_read_2 > 0,
            f"cache_create={cache_create_1}, cache_read={cache_read_2}",
        )
    except Exception as e:
        report("B", "Prompt caching", False, str(e))


# ---------------------------------------------------------------------------
# Test C: Langfuse SDK v3 integration
# ---------------------------------------------------------------------------
def test_c():
    """Can we create traces and generations with Langfuse SDK v3?"""
    try:
        from langfuse import Langfuse

        lf = Langfuse()

        # v3 API: start_span creates a trace implicitly
        span = lf.start_span(name="v2-validation-test")
        generation = lf.start_observation(
            as_type="generation",
            name="test-generation",
            model="claude-sonnet-4-5-20250929",
            input=[{"role": "user", "content": "test"}],
            output="test response",
            usage_details={
                "input": 10,
                "output": 5,
            },
        )
        generation.end()
        span.end()
        lf.flush()
        time.sleep(3)

        # Auth check verifies connection works
        lf.auth_check()

        lf.shutdown()

        report(
            "C",
            "Langfuse SDK v3 integration",
            True,
            "auth_check passed, span + generation created",
        )
    except Exception as e:
        report("C", "Langfuse SDK v3 integration", False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test D: Agent extraction pattern (multi-turn then extract)
# ---------------------------------------------------------------------------
def test_d():
    """Simulate 2-turn agent loop, then one extraction call with structured output."""
    try:
        messages = [
            {"role": "user", "content": "What are Portugal's two largest parties?"}
        ]

        # Turn 1: agent responds freely
        r1 = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=300,
            messages=messages,
        )
        assistant_text = r1.content[0].text
        messages.append({"role": "assistant", "content": assistant_text})

        # Turn 2: user follow-up
        messages.append({"role": "user", "content": "What are their ideologies?"})
        r2 = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=300,
            messages=messages,
        )
        assistant_text_2 = r2.content[0].text
        messages.append({"role": "assistant", "content": assistant_text_2})

        # Extraction call: structured output from conversation
        messages.append(
            {
                "role": "user",
                "content": "Extract the parties and ideologies into structured JSON.",
            }
        )

        r3 = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=500,
            messages=messages,
            output_config={"format": PARTIES_SCHEMA},
        )

        parsed = json.loads(r3.content[0].text)
        has_parties = isinstance(parsed.get("parties"), list) and len(parsed["parties"]) >= 2

        report(
            "D",
            "Agent extraction pattern",
            has_parties,
            f"turns=3, parties_extracted={len(parsed.get('parties', []))}",
        )
    except Exception as e:
        report("D", "Agent extraction pattern", False, str(e))


# ---------------------------------------------------------------------------
# Test E: Strict tool schemas
# ---------------------------------------------------------------------------
def test_e():
    """Does Claude respect strict tool schemas exactly?"""
    try:
        tools = [
            {
                "name": "search_initiatives",
                "description": "Search parliamentary initiatives.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "keywords": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Search keywords",
                        },
                        "party": {
                            "type": "string",
                            "description": "Party abbreviation (e.g., PS, PSD)",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results",
                        },
                    },
                    "required": ["keywords"],
                    "additionalProperties": False,
                },
            }
        ]

        response = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=300,
            tools=tools,
            messages=[
                {
                    "role": "user",
                    "content": "Search for housing initiatives by PSD, limit 5.",
                }
            ],
        )

        tool_calls = [b for b in response.content if b.type == "tool_use"]
        if not tool_calls:
            report("E", "Strict tool schemas", False, "No tool call made")
            return

        tc = tool_calls[0]
        inp = tc.input
        has_keywords = isinstance(inp.get("keywords"), list) and len(inp["keywords"]) > 0
        valid_keys = set(inp.keys()) <= {"keywords", "party", "limit"}

        report(
            "E",
            "Strict tool schemas",
            has_keywords and valid_keys,
            f"keywords={inp.get('keywords')}, party={inp.get('party')}, "
            f"limit={inp.get('limit')}, valid_keys={valid_keys}",
        )
    except Exception as e:
        report("E", "Strict tool schemas", False, str(e))


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("\nParlamentor v2 - Architecture Validation Tests")
    print("=" * 60)

    test_a()
    test_b()
    test_c()
    test_d()
    test_e()

    print("\n\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    for test_id, name, status in results:
        print(f"  {status} {test_id}: {name}")
    passed = sum(1 for _, _, s in results if s == PASS)
    print(f"\n  {passed}/{len(results)} tests passed")
    print("=" * 60)
