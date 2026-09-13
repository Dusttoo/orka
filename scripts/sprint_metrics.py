"""Observed sprint outcomes; reported completion is distinct from verified merge."""
from datetime import datetime, timezone

BLOCKED = {"blocked", "external_blocked", "operator_decision", "user_action", "needs_repair", "recoverable"}


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return None


def summarize(state, spend, as_of=None):
    as_of = as_of or datetime.now(timezone.utc)
    tickets = state["tickets"]
    attempted = [t for t in tickets.values() if t.get("attempts", 0) > 0]
    completed = [t for t in attempted if t.get("state") == "completed"]
    total = sum(float(spend.get(key, {}).get("spent_usd", 0)) for key in tickets)
    pr_opened = [ticket for ticket in attempted if ticket.get("pr")]
    ci_progressed = [
        ticket for ticket in attempted
        if ticket.get("ci_progress") or any(
            event.get("milestone") == "ci_advanced"
            for event in ticket.get("progress", [])
        )
    ]
    unfinished_prs = [
        ticket for ticket in pr_opened
        if ticket.get("state") not in {"completed", "decomposed"}
    ]
    timeout_stops = sum(
        1
        for ticket in tickets.values()
        for event in ticket.get("history", [])
        if event.get("event") == "supervisor-stopped"
        and str(event.get("reason") or "").startswith("max_worker_")
    )
    durations, decisions = {}, 0
    for key, ticket in tickets.items():
        previous, previous_at = None, None
        per_state = {}
        for event in ticket.get("history", []):
            at = timestamp(event.get("at"))
            kind = event.get("event")
            current = event.get("state")
            if kind in {"reserved", "batch-reserved"}:
                current = "running"
            elif kind == "finished":
                current = event.get("outcome")
            elif kind in {"requeued", "terminal-recovered", "legacy-recovered", "batch-failed-requeued"}:
                current = "pending"
            if not current or at is None or at > as_of or (previous_at and at < previous_at):
                continue
            if previous is not None:
                per_state[previous] = per_state.get(previous, 0) + (at - previous_at).total_seconds()
            if current in {"operator_decision", "user_action"} and current != previous:
                decisions += 1
            previous, previous_at = current, at
        if previous_at:
            per_state[previous] = per_state.get(previous, 0) + (as_of - previous_at).total_seconds()
        durations[key] = per_state or None
    return dict(attempted_tickets=len(attempted), reported_completed_tickets=len(completed),
        reported_completion_rate=len(completed)/len(attempted) if attempted else None,
        settled_spend_usd=round(total, 6),
        spend_per_reported_completion_usd=round(total/len(completed), 6) if completed else None,
        observed_operator_decision_entries=decisions,
        state_seconds_by_ticket=durations,
        observed_blocked_seconds=sum(sum(seconds for status, seconds in values.items() if status in BLOCKED)
                                     for values in durations.values() if values),
        pipeline={
            "attempted": len(attempted),
            "pr_opened": len(pr_opened),
            "ci_progress_recorded": len(ci_progressed),
            "merged_or_completed": len(completed),
            "unfinished_prs": len(unfinished_prs),
            "worker_timeout_stops": timeout_stops,
        },
        spend_coverage={
            "api_ledger_usd": round(total, 6),
            "desktop_subscription_included": False,
            "note": "Desktop/subscription model usage is not priced in the API usage ledger.",
        },
        as_of=as_of.isoformat(), verified_merged_tickets=None, spend_per_verified_merge_usd=None,
        coverage="Durations and decision entries cover recorded state transitions only; missing history is unknown.")


def verify_merges(root, state, metrics):
    from github_progress import repository, number_from_evidence, command_json, ProgressError
    host, name, repo_id = repository(root)
    merged, errors, cache = [], {}, {}
    for key, ticket in state["tickets"].items():
        if not ticket.get("attempts") or ticket.get("state") != "completed":
            continue
        try:
            number = number_from_evidence(str(ticket.get("pr", "")), host, name)
            if number not in cache:
                cache[number] = command_json(root, host, f"repos/{name}/pulls/{number}")
            pr = cache[number]
            if (pr.get("base", {}).get("repo", {}).get("id") != repo_id
                    or pr.get("head", {}).get("ref") != ticket.get("branch")
                    or pr.get("number") != number):
                raise ProgressError("PR identity does not match completed ticket")
            if pr.get("merged") is True and pr.get("merged_at") and pr.get("merge_commit_sha"):
                merged.append(dict(ticket=key, pr=number, merged_at=pr["merged_at"], commit=pr["merge_commit_sha"]))
        except (ProgressError, TypeError, AttributeError) as exc:
            errors[key] = str(exc)
    unique = len({item["pr"] for item in merged})
    metrics.update(verified_merged_tickets=len(merged), verified_unique_merges=unique,
        verified_merge_receipts=merged, merge_verification_errors=errors,
        spend_per_verified_merge_usd=round(metrics["settled_spend_usd"]/unique, 6) if unique else None)
    return metrics
