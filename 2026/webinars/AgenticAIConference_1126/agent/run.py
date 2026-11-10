"""Run one question through the agent.

    python -m agent.run --mode before --user bob "What is our ARR?"
    python -m agent.run --mode after  --user bob "What is our ARR?"
"""

import argparse
import asyncio
from contextlib import AsyncExitStack

from rich.panel import Panel

from . import config, llm
from .memory import AuditedSession
from .modes import after_mode, before_mode


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["before", "after"], required=True)
    p.add_argument("--user", default="alice")
    p.add_argument(
        "--no-review-hold",
        action="store_true",
        help="After mode only: share notes even if the session read untrusted input.",
    )
    p.add_argument(
        "--no-memory-filter",
        action="store_true",
        help="After mode only: recall all team notes, ignoring the reader's clearances.",
    )
    p.add_argument("question")
    args = p.parse_args()
    config.require("GCP_PROJECT_ID", "ANTHROPIC_API_KEY")

    llm.console.rule(f"[bold]{args.mode.upper()}[/bold] · user={args.user}")
    if args.mode == "before":
        system, tools = await before_mode(args.user)
        answer = await llm.run(system, args.question, tools)
    else:
        config.require("NEO4J_PASSWORD")
        async with AsyncExitStack() as stack:
            session = await stack.enter_async_context(
                AuditedSession(
                    args.user,
                    args.question,
                    review_hold=not args.no_review_hold,
                    memory_filter=not args.no_memory_filter,
                )
            )
            system, tools = await after_mode(args.user, session, stack)
            answer = await llm.run(system, args.question, tools)
            await session.finish(answer)
            llm.console.print(f"[dim]trace: {session.trace.id}[/dim]")
    llm.console.print(Panel(answer, title="Answer", border_style="bold"))


if __name__ == "__main__":
    asyncio.run(main())
