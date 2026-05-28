"""``/clusters`` — show the failure clusters the reflector has accumulated.

The ``failure_clustering`` reflector strategy groups recent failed or
far-from-target runs by error signature and stamps the result onto
``memory["failure_clusters"]`` at the end of each iteration. The
reflective reviewer reads that list on the next iteration to decide
whether to escalate the loop's strategy to ``"explore"``.

That signal flow is invisible to the user without a way to look at it.
This command surfaces the current clusters so users can see *why* the
loop wants to widen its search — count, signature, and the worst
sample entries per cluster.

No arguments: prints a compact summary. ``/clusters <signature>`` shows
all stored entries for one cluster (truncated to the first 3, since
that's what the reflector keeps — see ``build_failure_clusters``).
"""

from __future__ import annotations

import json
from textwrap import fill

from arc.chat.registry import SlashCommand
from arc.chat.state import ChatState
from arc.chat.ui import BOLD, CYAN, DIM, GREEN, RED, YELLOW, c, header, step, warn


def _clusters(state: ChatState) -> list[dict]:
    raw = state.memory.get("failure_clusters")
    if not isinstance(raw, list):
        return []
    return [c for c in raw if isinstance(c, dict)]


def _list_clusters(state: ChatState) -> None:
    clusters = _clusters(state)
    if not clusters:
        print(c("  No failure clusters recorded yet.", DIM))
        print(c("  Tip: /strategy reflector failure_clustering enables the "
                "clustering reflector,", DIM))
        print(c("       which populates this list at the end of each "
                "iteration.", DIM))
        return

    active_reflector = (state.memory.get("strategy_overrides") or {}).get("reflector")
    if active_reflector != "failure_clustering":
        warn(
            "These clusters were left by a previous reflector; the active "
            f"reflector is {c(str(active_reflector or 'default'), CYAN)}."
        )

    header(f"Failure clusters ({len(clusters)})")
    for cluster in clusters:
        count = cluster.get("count", 0)
        sig = cluster.get("signature") or "?"
        reason = cluster.get("reason") or sig
        # Colour by severity: ≥3 entries is loud, 2 is yellow, 1 dim.
        if count >= 3:
            count_col = RED
        elif count == 2:
            count_col = YELLOW
        else:
            count_col = DIM
        print(
            f"  {c(f'{count}×', count_col, BOLD)} {c(sig, CYAN, BOLD)}  "
            f"{c('— ' + reason, DIM)}"
        )

    print()
    print(c(f"  /clusters <signature> to see sample entries.", DIM))


def _show_cluster(state: ChatState, signature: str) -> None:
    clusters = _clusters(state)
    match = next((cl for cl in clusters if cl.get("signature") == signature), None)
    if match is None:
        # Try a prefix match so users can shorten long signatures.
        prefix_matches = [
            cl for cl in clusters
            if str(cl.get("signature", "")).startswith(signature)
        ]
        if len(prefix_matches) == 1:
            match = prefix_matches[0]
        elif len(prefix_matches) > 1:
            warn(
                f"{signature!r} matches multiple signatures; please be more specific."
            )
            for cl in prefix_matches:
                print(f"    {c('•', YELLOW)} {cl.get('signature')}")
            return

    if match is None:
        warn(f"No cluster with signature {signature!r}.")
        return

    header(f"Cluster: {match.get('signature', '?')}")
    step("Count",  str(match.get("count", 0)))
    step("Reason", fill(str(match.get("reason") or "—"), 60,
                        subsequent_indent=" " * 16))
    entries = match.get("entries") or []
    if not entries:
        print(c("  No sample entries recorded.", DIM))
        return
    step("Samples", "")
    for i, entry in enumerate(entries, 1):
        # Each entry is whatever shape was in run_history — usually
        # {inputs, outputs, status, ...}. Render as one indented JSON
        # block so even oddly-shaped entries print cleanly.
        try:
            body = json.dumps(entry, default=str, sort_keys=True, indent=2)
        except (TypeError, ValueError):
            body = repr(entry)
        for line in body.splitlines():
            print(f"    {c(f'[{i}] ' if line == body.splitlines()[0] else '    ', DIM)}{line}")


async def run(state: ChatState, argv: list[str]) -> None:
    if not argv:
        _list_clusters(state)
        return
    _show_cluster(state, argv[0])


COMMANDS = [
    SlashCommand(
        name="clusters",
        summary="Show failure clusters the reflector has accumulated.",
        handler=run,
        args_help="[<signature>]",
    ),
]
