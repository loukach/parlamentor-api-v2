"""Langfuse tracing helpers for agent observability.

Uses Langfuse SDK v3 direct: start_span() and start_observation(as_type='generation').
"""

import logging

from api.config import settings

logger = logging.getLogger(__name__)

_langfuse = None


def get_langfuse():
    """Lazy-init Langfuse client (reuses the one from main.py if available)."""
    global _langfuse
    if _langfuse is not None:
        return _langfuse

    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return None

    try:
        from langfuse import Langfuse
        _langfuse = Langfuse()
        return _langfuse
    except Exception:
        logger.exception("Failed to init Langfuse for tracing")
        return None


class TraceContext:
    """Manages a Langfuse trace for one agent turn (message -> response cycle)."""

    def __init__(self, investigation_id: str, stage: str, user_message: str):
        self.investigation_id = investigation_id
        self.stage = stage
        self._span = None
        self._langfuse = get_langfuse()

        if self._langfuse:
            try:
                self._span = self._langfuse.start_span(
                    name=f"{stage}: {user_message[:60]}",
                    input=user_message,
                    metadata={"stage": stage},
                )
                self._span.update_trace(
                    session_id=str(investigation_id),
                    input=user_message,
                )
            except Exception:
                logger.exception("Failed to start Langfuse span")

    def log_generation(
        self,
        model: str,
        input_messages: list[dict],
        output: str,
        usage: dict,
        iteration: int = 1,
    ) -> None:
        """Log an LLM generation (API call)."""
        if not self._span:
            return
        try:
            self._span.start_observation(
                as_type="generation",
                name=f"iteration-{iteration}",
                model=model,
                input=input_messages[-3:] if len(input_messages) > 3 else input_messages,
                output=output or "",
                usage_details={
                    "input": usage.get("input_tokens", 0),
                    "output": usage.get("output_tokens", 0),
                    "total": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                },
                metadata={
                    "cache_read_tokens": usage.get("cache_read_tokens", 0),
                    "cache_create_tokens": usage.get("cache_create_tokens", 0),
                    "cost_usd": usage.get("cost_usd", 0),
                },
            ).end()
        except Exception:
            logger.exception("Failed to log generation to Langfuse")

    def log_tool_call(
        self,
        tool_name: str,
        input_args: dict,
        output: dict,
        duration_ms: int,
    ) -> None:
        """Log a tool call as a span."""
        if not self._span:
            return
        try:
            tool_span = self._span.start_span(
                name=f"tool: {tool_name}",
                input=input_args,
                output={"row_count": output.get("count", 0), "error": output.get("error")},
                metadata={"duration_ms": duration_ms},
            )
            tool_span.end()
        except Exception:
            logger.exception("Failed to log tool call to Langfuse")

    def log_gate_decision(self, action: str, feedback: str | None) -> None:
        """Log a gate decision event."""
        if not self._span:
            return
        try:
            gate_span = self._span.start_span(
                name=f"gate: {action}",
                input={"action": action, "feedback": feedback},
            )
            gate_span.end()
        except Exception:
            logger.exception("Failed to log gate decision to Langfuse")

    def end(self, output: str | None = None) -> None:
        """End the trace span and flush."""
        if not self._span:
            return
        try:
            if output:
                # Store full output (Langfuse handles large payloads)
                self._span.update(output=output)
                # Trace output: use first 1000 chars as preview
                preview = output[:1000] + "..." if len(output) > 1000 else output
                self._span.update_trace(output=preview)
            self._span.end()
            if self._langfuse:
                self._langfuse.flush()
        except Exception:
            logger.exception("Failed to end Langfuse span")
