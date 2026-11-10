"""A deliberately small tool-use loop. Both modes use the same loop and model;
only the tools and the context they return differ."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from anthropic import AsyncAnthropic
from rich.console import Console
from rich.panel import Panel

from . import config

console = Console()

Handler = Callable[[dict], Awaitable[str]]


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Handler

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


def _preview(text: str, limit: int = 600) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n… ({len(text) - limit} more chars)"


_EPHEMERAL = {"type": "ephemeral"}


def _system_blocks(system: str) -> list[dict] | str:
    if not config.PROMPT_CACHING:
        return system
    return [{"type": "text", "text": system, "cache_control": _EPHEMERAL}]


def _tool_specs(tools: list[Tool]) -> list[dict]:
    specs = [t.spec() for t in tools]
    if config.PROMPT_CACHING and specs:
        specs[-1] = {**specs[-1], "cache_control": _EPHEMERAL}
    return specs


def _move_cache_breakpoint(messages: list[dict]) -> None:
    """Keep exactly one cache breakpoint in the conversation: on the newest user turn.
    Earlier turns are still read from cache (the API looks back for the longest cached
    prefix), and we stay within the 4-breakpoint limit (system + tools + this one)."""
    if not config.PROMPT_CACHING:
        return
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], list):
            for block in m["content"]:
                block.pop("cache_control", None)
    messages[-1]["content"][-1]["cache_control"] = _EPHEMERAL


def _cap(output: str) -> str:
    limit = config.MAX_TOOL_RESULT_CHARS
    if len(output) <= limit:
        return output
    return output[:limit] + f"\n[truncated: {len(output) - limit:,} more characters. Narrow your search.]"


async def run(
    system: str,
    question: str,
    tools: list[Tool],
    *,
    model: str | None = None,
    quiet: bool = False,
    transcript: dict | None = None,
) -> str:
    """Run the tool loop. `transcript`, if given, collects tool calls and token usage
    (used by the eval harness); `quiet` suppresses terminal output."""
    client = AsyncAnthropic(**config.anthropic_client_kwargs())
    by_name = {t.name: t for t in tools}
    messages: list[dict] = [{"role": "user", "content": [{"type": "text", "text": question}]}]
    usage_keys = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    if transcript is not None:
        transcript.setdefault("calls", [])
        for k in usage_keys:
            transcript.setdefault(k, 0)
    say = (lambda *a, **k: None) if quiet else console.print
    system_blocks, tool_specs = _system_blocks(system), _tool_specs(tools)

    for _ in range(config.AGENT_MAX_TURNS):
        _move_cache_breakpoint(messages)
        resp = await client.messages.create(
            model=model or config.AGENT_MODEL,
            max_tokens=2048,
            system=system_blocks,
            tools=tool_specs,
            messages=messages,
        )
        if transcript is not None:
            for k in usage_keys:
                transcript[k] += getattr(resp.usage, k, 0) or 0
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text").strip()

        results = []
        for block in resp.content:
            if block.type == "text" and block.text.strip():
                say(f"[dim]{block.text.strip()}[/dim]")
            if block.type != "tool_use":
                continue
            say(f"[bold cyan]→ {block.name}[/bold cyan] {json.dumps(block.input)}")
            tool = by_name.get(block.name)
            try:
                output = await tool.handler(block.input) if tool else f"Unknown tool {block.name}"
                is_error = False
            except Exception as e:  # surfaced to the model so it can recover
                output, is_error = f"ERROR: {e}", True
            style = "red" if is_error or output.startswith("DENIED") else "green"
            say(Panel(_preview(output), border_style=style, expand=False))
            if transcript is not None:
                transcript["calls"].append(
                    {"name": block.name, "input": block.input, "output": output[:4000], "error": is_error}
                )
            results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": _cap(output), "is_error": is_error}
            )
        messages.append({"role": "user", "content": results})

    return "Stopped: reached the turn limit without a final answer."
