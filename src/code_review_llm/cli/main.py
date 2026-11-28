from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from code_review_llm.db import ReviewStore, init_schema, open_connection
from code_review_llm.domain import (
    ContextPolicy,
    ExecutionMode,
    ReviewPolicy,
    ReviewRequest,
    RunStatus,
)
from code_review_llm.ingest import InputRejectedError, ingest_local_repo
from code_review_llm.llm import GeminiProvider, LLMProvider, OpenAICompatProvider, ReplayProvider
from code_review_llm.pipeline import OrchestratorConfig, ReviewOrchestrator, save_review_run

app = typer.Typer(name="code-review-llm", help="Hybrid static + LLM Python PR code reviewer.")


def _build_provider(provider_name: str) -> LLMProvider | None:
    low = provider_name.strip().lower()
    if low in ("none", ""):
        return None
    if low == "replay":
        return ReplayProvider()
    if low == "gemini":
        return GeminiProvider()
    if low == "groq":
        return OpenAICompatProvider(
            provider_name="groq",
            base_url="https://api.groq.com/openai/v1",
            env_var="GROQ_API_KEY",
        )
    if low == "openrouter":
        return OpenAICompatProvider(
            provider_name="openrouter",
            base_url="https://openrouter.ai/api/v1",
            env_var="OPENROUTER_API_KEY",
        )
    if low == "cerebras":
        return OpenAICompatProvider(
            provider_name="cerebras",
            base_url="https://api.cerebras.ai/v1",
            env_var="CEREBRAS_API_KEY",
        )
    raise typer.BadParameter(f"Unknown provider: {provider_name}")


def _delete_local_artifact(storage_uri: str) -> bool:
    raw = storage_uri[7:] if storage_uri.startswith("file://") else storage_uri
    target = Path(raw)
    if not target.exists():
        return True
    if target.is_dir():
        return False
    os.unlink(target)
    return True


@app.command("init-db")
def init_db_cmd(
    db: Annotated[str, typer.Option("--db", help="SQLite database path")] = "code_review_llm.db",
) -> None:
    conn = open_connection(db)
    try:
        init_schema(conn)
    finally:
        conn.close()
    typer.echo(f"Initialized database at {db}")


@app.command("review")
def review_cmd(
    repo_path: Annotated[str, typer.Argument(help="Path to local git repository")] = ".",
    base: Annotated[str, typer.Option("--base", help="Base git ref")] = "HEAD~1",
    head: Annotated[str, typer.Option("--head", help="Head git ref")] = "HEAD",
    working_tree: Annotated[bool, typer.Option("--working-tree", help="Review uncommitted diff")] = False,
    mode: Annotated[ExecutionMode, typer.Option("--mode", help="Execution mode")] = ExecutionMode.D_HYBRID,
    context_policy: Annotated[
        ContextPolicy, typer.Option("--context-policy", help="Context policy")
    ] = ContextPolicy.DIFF_AST_INTENT,
    provider: Annotated[str, typer.Option("--provider", help="LLM provider name")] = "replay",
    model: Annotated[str | None, typer.Option("--model", help="Model ID")] = None,
    allow_external_llm: Annotated[
        bool, typer.Option("--allow-external-llm", help="Allow outbound API calls")
    ] = False,
    no_verify: Annotated[bool, typer.Option("--no-verify", help="Disable verification filter")] = False,
    max_files: Annotated[int, typer.Option("--max-files", min=1)] = 10,
    max_diff_lines: Annotated[int, typer.Option("--max-diff-lines", min=1)] = 800,
    max_input_tokens: Annotated[int, typer.Option("--max-input-tokens", min=0)] = 12000,
    max_llm_calls: Annotated[int, typer.Option("--max-llm-calls", min=0)] = 6,
    timeout: Annotated[int, typer.Option("--timeout", min=1)] = 300,
    db: Annotated[str, typer.Option("--db", help="SQLite database path")] = "code_review_llm.db",
    output: Annotated[str | None, typer.Option("--output", help="Write JSON report file")] = None,
    json_out: Annotated[bool, typer.Option("--json-out", help="Print JSON report to stdout")] = False,
) -> None:
    policy = ReviewPolicy(
        max_files_limit=max_files,
        max_diff_lines=max_diff_lines,
        max_input_tokens=max_input_tokens,
        max_llm_calls=max_llm_calls,
        timeout_seconds=timeout,
        allow_external_llm=allow_external_llm,
    )
    try:
        ingest = ingest_local_repo(
            Path(repo_path),
            base_ref=base,
            head_ref=head,
            include_working_tree=working_tree,
            policy=policy,
        )
    except InputRejectedError as exc:
        typer.echo(f"Input rejected: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    llm_provider = None if mode == ExecutionMode.A_STATIC else _build_provider(provider)
    orchestrator = ReviewOrchestrator(
        provider=llm_provider,
        config=OrchestratorConfig(enable_verification=not no_verify),
    )
    request = ReviewRequest(
        repo_full_name=ingest.snapshot.repo_full_name,
        execution_mode=mode,
        context_policy=context_policy,
        policy=policy,
        llm_model_name=model,
    )
    report = orchestrator.run(ingest, request)

    if db and db.lower() != "none":
        conn = open_connection(db)
        try:
            init_schema(conn)
            store = ReviewStore(conn)
            report = save_review_run(store, request, report)
        finally:
            conn.close()

    serialized = report.model_dump_json(indent=2)
    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(serialized, encoding="utf-8")

    if json_out:
        typer.echo(serialized)
    else:
        typer.echo(
            f"Run #{report.run_id or '-'} [{report.execution_mode.value}] "
            f"status={report.run_status.value} findings={len(report.findings)}"
        )

    if report.run_status == RunStatus.FAILED:
        raise typer.Exit(code=1)


@app.command("history")
def history_cmd(
    db: Annotated[str, typer.Option("--db", help="SQLite database path")] = "code_review_llm.db",
    limit: Annotated[int, typer.Option("--limit", min=1)] = 20,
    json_out: Annotated[bool, typer.Option("--json-out", help="Print JSON output")] = False,
) -> None:
    conn = open_connection(db)
    try:
        init_schema(conn)
        runs = ReviewStore(conn).list_runs(limit=limit)
    finally:
        conn.close()
    if json_out:
        typer.echo(json.dumps(runs, indent=2))
        return
    if not runs:
        typer.echo("No review runs recorded.")
        return
    for r in runs:
        typer.echo(
            f"#{r['run_id']} {r['repo_full_name']} [{r['execution_mode']}] "
            f"status={r['run_status']} findings={r['finding_count']}"
        )


@app.command("export")
def export_cmd(
    run_id: Annotated[int, typer.Argument(help="Review run ID")],
    db: Annotated[str, typer.Option("--db", help="SQLite database path")] = "code_review_llm.db",
    output: Annotated[str | None, typer.Option("--output", help="Output JSON file path")] = None,
) -> None:
    conn = open_connection(db)
    try:
        init_schema(conn)
        detail = ReviewStore(conn).get_run_detail(run_id)
    finally:
        conn.close()
    if detail is None:
        typer.echo(f"Run #{run_id} not found", err=True)
        raise typer.Exit(code=1)
    payload = json.dumps(detail, indent=2)
    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload, encoding="utf-8")
        typer.echo(f"Exported run #{run_id} to {output}")
    else:
        typer.echo(payload)


@app.command("purge")
def purge_cmd(
    db: Annotated[str, typer.Option("--db", help="SQLite database path")] = "code_review_llm.db",
    days_offset: Annotated[int, typer.Option("--days-offset", help="Advance clock by N days")] = 0,
) -> None:
    conn = open_connection(db)
    try:
        init_schema(conn)
        now = datetime.now(UTC) + timedelta(days=days_offset)
        counts = ReviewStore(conn).purge_expired_artifacts(now, _delete_local_artifact)
    finally:
        conn.close()
    typer.echo(json.dumps(counts, indent=2))
