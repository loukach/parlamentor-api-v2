"""Agent executor: async generator that yields WebSocket message dicts.

The WebSocket handler iterates and sends each to the client.
Critical: build assistant message content blocks explicitly - never use model_dump().
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator

import anthropic

from api.config import settings
from api.costs import calculate_cost

logger = logging.getLogger(__name__)

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    """Lazy-init the Anthropic client (avoids empty key at import time)."""
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


async def run_agent(
    *,
    model: str,
    system_prompt: list[dict],
    messages: list[dict],
    tools: list[dict],
    tool_handlers: dict[str, callable],
    thinking: dict | None = None,
    cancel_event: asyncio.Event,
) -> AsyncGenerator[dict, None]:
    """Run the agent loop, yielding WS message dicts.

    Yields:
        text_delta, tool_call, tool_result, thinking, usage, agent_done
    """
    iteration = 0

    while True:
        if cancel_event.is_set():
            return

        iteration += 1
        t0 = time.monotonic()

        # Build API call kwargs
        api_kwargs: dict = {
            "model": model,
            "max_tokens": 16384,
            "system": system_prompt,
            "messages": messages,
            "tools": tools,
        }
        if thinking:
            api_kwargs["thinking"] = thinking

        # Stream the response
        collected_content: list[dict] = []
        stop_reason = None
        usage_data = {}

        try:
            async with _get_client().messages.stream(**api_kwargs) as stream:
                async for event in stream:
                    if cancel_event.is_set():
                        return

                    if event.type == "content_block_start":
                        block = event.content_block
                        if block.type == "thinking":
                            collected_content.append({"type": "thinking", "thinking": ""})
                        elif block.type == "text":
                            collected_content.append({"type": "text", "text": ""})
                        elif block.type == "tool_use":
                            collected_content.append({
                                "type": "tool_use",
                                "id": block.id,
                                "name": block.name,
                                "input": {},
                            })

                    elif event.type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "thinking_delta":
                            collected_content[-1]["thinking"] += delta.thinking
                            yield {"type": "thinking", "content": delta.thinking}
                        elif delta.type == "text_delta":
                            collected_content[-1]["text"] += delta.text
                            yield {"type": "text_delta", "content": delta.text}
                        elif delta.type == "input_json_delta":
                            # Accumulate JSON for tool input (we'll parse it at the end)
                            pass

                    elif event.type == "message_delta":
                        stop_reason = event.delta.stop_reason
                        if hasattr(event, "usage") and event.usage:
                            usage_data["output_tokens"] = event.usage.output_tokens

                    elif event.type == "message_start":
                        if hasattr(event.message, "usage") and event.message.usage:
                            usage_data["input_tokens"] = event.message.usage.input_tokens
                            usage_data["cache_read_tokens"] = getattr(
                                event.message.usage, "cache_read_input_tokens", 0
                            ) or 0
                            usage_data["cache_create_tokens"] = getattr(
                                event.message.usage, "cache_creation_input_tokens", 0
                            ) or 0

                # Get the final message for accurate tool inputs
                final_message = await stream.get_final_message()

        except anthropic.APIError as e:
            logger.error("Anthropic API error: %s", e)
            yield {"type": "error", "content": f"API error: {e.message}"}
            return

        duration_ms = int((time.monotonic() - t0) * 1000)

        # Rebuild content blocks from final message (avoids partial JSON issues)
        assistant_content = []
        for block in final_message.content:
            if block.type == "thinking":
                assistant_content.append({
                    "type": "thinking",
                    "thinking": block.thinking,
                })
            elif block.type == "text":
                assistant_content.append({
                    "type": "text",
                    "text": block.text,
                })
            elif block.type == "tool_use":
                assistant_content.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })

        # Extract usage from final message
        if final_message.usage:
            usage_data["input_tokens"] = final_message.usage.input_tokens
            usage_data["output_tokens"] = final_message.usage.output_tokens
            usage_data["cache_read_tokens"] = getattr(
                final_message.usage, "cache_read_input_tokens", 0
            ) or 0
            usage_data["cache_create_tokens"] = getattr(
                final_message.usage, "cache_creation_input_tokens", 0
            ) or 0

        stop_reason = final_message.stop_reason

        # Calculate cost
        cost = calculate_cost(
            model,
            usage_data.get("input_tokens", 0),
            usage_data.get("output_tokens", 0),
            usage_data.get("cache_read_tokens", 0),
            usage_data.get("cache_create_tokens", 0),
        )

        # Yield usage
        yield {
            "type": "usage",
            "input_tokens": usage_data.get("input_tokens", 0),
            "output_tokens": usage_data.get("output_tokens", 0),
            "cache_read_tokens": usage_data.get("cache_read_tokens", 0),
            "cache_create_tokens": usage_data.get("cache_create_tokens", 0),
            "cost_usd": cost,
            "iteration": iteration,
            "duration_ms": duration_ms,
            "model": model,
        }

        # Append assistant message to conversation
        messages.append({"role": "assistant", "content": assistant_content})

        # Handle stop reason
        if stop_reason == "end_turn":
            yield {"type": "agent_done"}
            return

        if stop_reason == "tool_use":
            # Dispatch all tool calls
            tool_results = []
            tool_call_count = 0

            for block in assistant_content:
                if block["type"] != "tool_use":
                    continue

                if cancel_event.is_set():
                    return

                tool_name = block["name"]
                tool_input = block["input"]
                tool_id = block["id"]
                tool_call_count += 1

                yield {
                    "type": "tool_call",
                    "tool": tool_name,
                    "input": tool_input,
                    "summary": _tool_summary(tool_name, tool_input),
                }

                handler = tool_handlers.get(tool_name)
                if handler:
                    try:
                        result_data = await handler(tool_input)
                    except Exception as e:
                        logger.error("Tool %s failed: %s", tool_name, e)
                        result_data = {"error": str(e)}
                else:
                    result_data = {"error": f"Unknown tool: {tool_name}"}

                result_str = json.dumps(result_data, ensure_ascii=False, default=str)

                # Yield tool result
                yield {
                    "type": "tool_result",
                    "tool": tool_name,
                    "summary": _result_summary(tool_name, result_data),
                    "row_count": result_data.get("count"),
                    "gate_requested": tool_name == "request_gate_review",
                }

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": result_str,
                })

            # Append tool results to conversation
            messages.append({"role": "user", "content": tool_results})

            # Continue the loop (agent will process tool results)
            continue

        # Unexpected stop reason
        logger.warning("Unexpected stop_reason: %s", stop_reason)
        yield {"type": "agent_done"}
        return


async def run_extraction(
    model: str,
    messages: list[dict],
    schema: dict,
    extraction_prompt: str,
) -> tuple[dict, dict]:
    """Run structured extraction. Returns (parsed_output, usage)."""
    extraction_messages = messages + [
        {"role": "user", "content": extraction_prompt}
    ]

    # If schema has a wrapper (name/strict/schema keys), unwrap to the plain JSON Schema.
    actual_schema = schema.get("schema", schema) if "schema" in schema else schema

    response = await _get_client().messages.create(
        model=model,
        max_tokens=8192,
        messages=extraction_messages,
        output_config={"format": {"type": "json_schema", "schema": actual_schema}},
    )

    # Parse the response text as JSON
    output_text = ""
    for block in response.content:
        if block.type == "text":
            output_text += block.text

    parsed = json.loads(output_text)

    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_read_tokens": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        "cache_create_tokens": getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
        "model": model,
    }

    return parsed, usage


def _tool_summary(tool_name: str, tool_input: dict) -> str:
    """Generate a human-readable summary of a tool call."""
    if tool_name == "search_initiatives":
        kw = ", ".join(tool_input.get("keywords", []))
        party = tool_input.get("party", "")
        parts = [f"keywords: {kw}"] if kw else []
        if party:
            parts.append(f"partido: {party}")
        return f"A pesquisar iniciativas ({', '.join(parts)})" if parts else "A pesquisar iniciativas"

    if tool_name == "search_votes":
        parts = []
        if tool_input.get("initiative_id"):
            parts.append(f"iniciativa #{tool_input['initiative_id']}")
        if tool_input.get("party"):
            parts.append(f"partido: {tool_input['party']}")
        if tool_input.get("keywords"):
            parts.append(f"keywords: {', '.join(tool_input['keywords'])}")
        return f"A pesquisar votacoes ({', '.join(parts)})" if parts else "A pesquisar votacoes"

    if tool_name == "search_deputies":
        parts = []
        if tool_input.get("name"):
            parts.append(tool_input["name"])
        if tool_input.get("party"):
            parts.append(f"partido: {tool_input['party']}")
        return f"A pesquisar deputados ({', '.join(parts)})" if parts else "A pesquisar deputados"

    if tool_name == "describe_table":
        return f"A consultar esquema: {tool_input.get('table_name', '?')}"

    if tool_name == "raw_query":
        return f"Query SQL: {tool_input.get('description', 'custom query')}"

    if tool_name == "request_gate_review":
        return "A solicitar revisao do jornalista"

    return f"A executar {tool_name}"


def _result_summary(tool_name: str, result_data: dict) -> str:
    """Generate a human-readable summary of a tool result."""
    if "error" in result_data:
        return f"Erro: {result_data['error']}"

    count = result_data.get("count", 0)
    desc = result_data.get("query_description", "")

    if tool_name == "describe_table":
        table = result_data.get("table", "")
        return f"{count} coluna(s) - {table}"

    if tool_name == "request_gate_review":
        return "Revisao solicitada"

    return f"{count} resultado(s) encontrado(s)" + (f" - {desc}" if desc else "")
