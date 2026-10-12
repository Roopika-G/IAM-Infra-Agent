"""Optional tracing to Phoenix (a local viewer for LLM runs, http://localhost:6006).

Off unless PHOENIX_COLLECTOR_ENDPOINT is set, and harmless if the Phoenix
packages are not installed (`uv sync --extra tracing`). Phoenix automatically
captures LangGraph nodes, every LLM call (prompt, answer, tokens, time) and the
tools the model calls. The tool calls our own code makes (intake, applying the
fix) go through tool_span() so they show up too.
"""

import contextlib
import json
import os
import sys


def setup_tracing() -> bool:
    """Start sending traces to Phoenix. Call once per process, before the agent runs."""
    endpoint = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT")
    if not endpoint:
        return False
    try:
        from phoenix.otel import register
    except ImportError:
        print("tracing: Phoenix is not installed (uv sync --extra tracing); running without it", file=sys.stderr)
        return False
    # batch=False exports each span as soon as it ends, so the view is close to live.
    register(project_name=os.environ.get("PHOENIX_PROJECT", "iam-agent"), endpoint=endpoint.rstrip("/") + "/v1/traces",
             auto_instrument=True, verbose=False)
    return True


def _tracer():
    try:
        from opentelemetry import trace
    except ImportError:
        return None
    return trace.get_tracer("iam-agent")


@contextlib.contextmanager
def run_span(incident_id: int, label: str = "agent run"):
    """One parent span around a whole agent run, so Phoenix shows the run as a
    single trace (graph nodes, LLM calls and tool calls nested inside) instead
    of dozens of unrelated ones. A no-op when tracing is off."""
    tracer = _tracer()
    if tracer is None:
        yield
        return
    with tracer.start_as_current_span(f"{label}: incident #{incident_id}") as span:
        span.set_attribute("openinference.span.kind", "AGENT")
        span.set_attribute("incident.id", incident_id)
        span.set_attribute("session.id", f"incident-{incident_id}")
        yield


@contextlib.contextmanager
def tool_span(name: str, args: dict):
    """A Phoenix span around one MCP tool call. Yields a recorder, or a no-op if tracing is off."""
    tracer = _tracer()
    if tracer is None:
        yield lambda result, error: None
        return
    with tracer.start_as_current_span(f"tool: {name}") as span:
        span.set_attribute("openinference.span.kind", "TOOL")
        span.set_attribute("tool.name", name)
        span.set_attribute("input.value", json.dumps(args, default=str)[:4000])

        def record(result, error):
            span.set_attribute("output.value", (f"ERROR: {error}" if error else json.dumps(result, default=str))[:4000])

        yield record
