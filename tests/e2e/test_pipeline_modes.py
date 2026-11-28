from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from code_review_llm.cli.main import app
from code_review_llm.db import ReviewStore, init_schema, open_connection
from code_review_llm.domain import (
    AssessmentStatus,
    CallStatus,
    Category,
    ChangedFile,
    ChangeType,
    CodeSnapshot,
    ContextPolicy,
    DiffSide,
    EvidenceStatus,
    ExecutionMode,
    FilterStatus,
    FindingKind,
    FixVerificationStatus,
    InputSource,
    PatchStatus,
    ReviewFinding,
    ReviewPolicy,
    ReviewRequest,
    RunStatus,
    Severity,
    SourceType,
)
from code_review_llm.ingest import IngestResult, ingest_snapshot_pair
from code_review_llm.llm import (
    LLMCandidateItem,
    LLMReviewOutput,
    LLMVerifyDecision,
    LLMVerifyOutput,
    OpenAICompatProvider,
    ProviderResponse,
    ReplayProvider,
)
from code_review_llm.pipeline import (
    OrchestratorConfig,
    ReviewOrchestrator,
    save_review_run,
    verify_suggested_fix,
)

BASE_FILE = (
    "def legacy_helper(arg):\n"
    "    try:\n"
    "        return int(arg)\n"
    "    except:\n"
    "        return 0\n\n"
    "def get_user(conn, name):\n"
    "    return conn.execute('SELECT * FROM users WHERE name = ?', (name,)).fetchall()\n"
)

HEAD_FILE = (
    "def legacy_helper(arg):\n"
    "    try:\n"
    "        return int(arg)\n"
    "    except:\n"
    "        return 0\n\n"
    "def get_user(conn, name, cache=[]):\n"
    "    query = f\"SELECT * FROM users WHERE name = '{name}'\"\n"
    "    cache.append(name)\n"
    "    return conn.execute(query).fetchall()\n"
)


def _sample_llm_review() -> str:
    return LLMReviewOutput(
        findings=[
            LLMCandidateItem(
                file_path="app.py",
                diff_side=DiffSide.RIGHT,
                start_line=8,
                end_line=10,
                category=Category.SECURITY,
                finding_kind=FindingKind.DEFECT,
                severity=Severity.HIGH,
                evidence_status=EvidenceStatus.GROUNDED,
                title="SQL injection via formatted query",
                explanation="Interpolating name into SQL string inside `get_user()` enables injection.",
                code_evidence="query = f\"SELECT * FROM users WHERE name = '{name}'\"",
                suggested_fix='conn.execute("SELECT * FROM users WHERE name = ?", (name,))',
            ),
            LLMCandidateItem(
                file_path="app.py",
                diff_side=DiffSide.RIGHT,
                start_line=7,
                end_line=7,
                category=Category.FUNCTIONAL,
                finding_kind=FindingKind.DEFECT,
                severity=Severity.HIGH,
                evidence_status=EvidenceStatus.GROUNDED,
                title="Mutable default argument cache",
                explanation="Default list in `get_user()` is mutated across invocations.",
                code_evidence="def get_user(conn, name, cache=[]):",
            ),
        ]
    ).model_dump_json()


def test_four_execution_modes_end_to_end_and_persistence(tmp_path: Path) -> None:
    ingest = ingest_snapshot_pair(
        repo_full_name="local/pyrev",
        base_files={"app.py": BASE_FILE},
        head_files={"app.py": HEAD_FILE},
    )

    conn = open_connection(tmp_path / "modes.db")
    init_schema(conn)
    store = ReviewStore(conn)

    req_a = ReviewRequest(repo_full_name="local/pyrev", execution_mode=ExecutionMode.A_STATIC)
    rep_a = ReviewOrchestrator().run(ingest, req_a)
    rep_a = save_review_run(store, req_a, rep_a)
    assert rep_a.run_status == RunStatus.COMPLETED
    assert rep_a.context_policy == ContextPolicy.NOT_APPLICABLE
    assert len(rep_a.invocations) == 0
    assert any(c.filter_status == FilterStatus.PRE_EXISTING for c in rep_a.candidates)
    assert len(rep_a.findings) == 2
    assert {f.category for f in rep_a.findings} == {Category.SECURITY, Category.FUNCTIONAL}
    cfg_row = conn.execute(
        "SELECT prompt_id, llm_model_name FROM ReviewConfig rc "
        "JOIN ReviewRun rr ON rr.config_id = rc.config_id WHERE rr.run_id = ?",
        (rep_a.run_id,),
    ).fetchone()
    assert cfg_row["prompt_id"] is None
    assert cfg_row["llm_model_name"] is None

    rep_a_idem = save_review_run(store, req_a, rep_a)
    assert rep_a_idem.run_id == rep_a.run_id

    prov_b = ReplayProvider(queued_responses=[_sample_llm_review()])
    req_b = ReviewRequest(repo_full_name="local/pyrev", execution_mode=ExecutionMode.B_LLM)
    rep_b = ReviewOrchestrator(
        provider=prov_b,
        config=OrchestratorConfig(enable_verification=False),
    ).run(ingest, req_b)
    rep_b = save_review_run(store, req_b, rep_b)
    assert "<static_signals>" not in prov_b.calls[0][2]
    assert rep_b.run_status == RunStatus.COMPLETED
    assert len(rep_b.invocations) == 1
    assert len(rep_b.findings) == 2
    assert all(f.sources == [SourceType.LLM] for f in rep_b.findings)

    prov_c = ReplayProvider(queued_responses=[_sample_llm_review()])
    req_c = ReviewRequest(repo_full_name="local/pyrev", execution_mode=ExecutionMode.C_CONCAT)
    rep_c = ReviewOrchestrator(provider=prov_c).run(ingest, req_c)
    rep_c = save_review_run(store, req_c, rep_c)
    assert "<static_signals>" not in prov_c.calls[0][2]
    assert len(rep_c.invocations) == 1
    assert len(rep_c.findings) > len(rep_b.findings)

    verify_d = LLMVerifyOutput(
        decisions=[
            LLMVerifyDecision(candidate_index=0, verdict="KEEP", reason="Confirmed SQLi"),
            LLMVerifyDecision(candidate_index=1, verdict="KEEP", reason="Confirmed mutable default"),
        ]
    ).model_dump_json()
    prov_d = ReplayProvider(queued_responses=[_sample_llm_review(), verify_d])
    req_d = ReviewRequest(repo_full_name="local/pyrev", execution_mode=ExecutionMode.D_HYBRID)
    rep_d = ReviewOrchestrator(provider=prov_d).run(ingest, req_d)
    rep_d = save_review_run(store, req_d, rep_d)
    assert "<static_signals>" in prov_d.calls[0][2]
    assert len(prov_d.calls) == 2
    assert rep_d.run_status == RunStatus.COMPLETED
    assert len(rep_d.findings) == 2
    sqli_f = next(f for f in rep_d.findings if f.category == Category.SECURITY)
    assert SourceType.LLM in sqli_f.sources
    assert len(sqli_f.sources) >= 2
    assert sqli_f.fix_verification_status == FixVerificationStatus.VERIFIED
    assert all(f.code_evidence.strip() for f in rep_d.findings)
    mut_f = next(f for f in rep_d.findings if f.category == Category.FUNCTIONAL)
    assert mut_f.fix_verification_status == FixVerificationStatus.NOT_APPLICABLE

    hunk_row = conn.execute("SELECT ast_symbol_name, ast_context_snippet FROM DiffHunk LIMIT 1").fetchone()
    assert hunk_row is not None
    assert hunk_row["ast_context_snippet"] is not None
    detail = store.get_run_detail(rep_d.run_id or 0)
    assert detail is not None
    assert len(detail["quality_assessments"]) == 5
    dup_rows = [c for c in detail["candidates"] if c["filter_status"] == "DROPPED_DUPLICATE"]
    assert len(dup_rows) >= 1
    assert all(c["finding_id"] is not None for c in dup_rows)
    conn.close()


def test_external_llm_block_timeout_partial_budget_and_empty_diff() -> None:
    ingest = ingest_snapshot_pair(
        repo_full_name="local/sec",
        base_files={"app.py": BASE_FILE},
        head_files={"app.py": HEAD_FILE},
    )

    ext_provider = OpenAICompatProvider(provider_name="groq", api_key="dummy-key")
    req_blocked = ReviewRequest(
        repo_full_name="local/sec",
        execution_mode=ExecutionMode.B_LLM,
        policy=ReviewPolicy(allow_external_llm=False),
    )
    rep_blocked = ReviewOrchestrator(provider=ext_provider).run(ingest, req_blocked)
    assert rep_blocked.run_status == RunStatus.FAILED
    assert rep_blocked.is_full_coverage is False
    assert all(
        qa.assessment_status == AssessmentStatus.INSUFFICIENT_INFO for qa in rep_blocked.quality_assessments
    )

    ticks = iter([0.0, 400.0])
    rep_timeout = ReviewOrchestrator(
        provider=ReplayProvider(),
        clock_fn=lambda: next(ticks, 500.0),
    ).run(ingest, ReviewRequest(repo_full_name="local/sec", execution_mode=ExecutionMode.D_HYBRID))
    assert rep_timeout.run_status == RunStatus.PARTIAL
    assert rep_timeout.is_full_coverage is False
    assert len(rep_timeout.findings) == 2
    qa_map = {q.dimension_name: q for q in rep_timeout.quality_assessments}
    assert qa_map[Category.SECURITY].assessment_status == AssessmentStatus.ISSUES_FOUND
    assert qa_map[Category.SECURITY].is_scope_complete is False
    assert qa_map[Category.CONVENTION].assessment_status == AssessmentStatus.INSUFFICIENT_INFO

    fail_prov = ReplayProvider(
        queued_responses=[
            ProviderResponse(
                call_status=CallStatus.PROVIDER_ERROR,
                resolved_model_name="replay-v1",
                error_detail="Service down",
                retryable=False,
            )
        ]
    )
    rep_fail_b = ReviewOrchestrator(provider=fail_prov).run(
        ingest,
        ReviewRequest(repo_full_name="local/sec", execution_mode=ExecutionMode.B_LLM),
    )
    assert rep_fail_b.run_status == RunStatus.FAILED
    assert all(
        qa.assessment_status != AssessmentStatus.NO_ISSUES_OBSERVED for qa in rep_fail_b.quality_assessments
    )

    req_tight = ReviewRequest(
        repo_full_name="local/sec",
        execution_mode=ExecutionMode.D_HYBRID,
        policy=ReviewPolicy(max_input_tokens=50),
    )
    rep_tight = ReviewOrchestrator(provider=ReplayProvider()).run(ingest, req_tight)
    assert rep_tight.run_status == RunStatus.PARTIAL
    assert rep_tight.is_full_coverage is False
    assert len(rep_tight.omitted_files) >= 1

    empty_ingest = ingest_snapshot_pair(
        repo_full_name="local/docs",
        base_files={"README.md": "hello\n"},
        head_files={"README.md": "world\n"},
    )
    rep_empty = ReviewOrchestrator(provider=ReplayProvider()).run(
        empty_ingest,
        ReviewRequest(repo_full_name="local/docs", execution_mode=ExecutionMode.D_HYBRID),
    )
    assert rep_empty.run_status == RunStatus.COMPLETED
    assert all(
        qa.assessment_status == AssessmentStatus.NOT_APPLICABLE for qa in rep_empty.quality_assessments
    )


def test_typer_cli_end_to_end_commands(tmp_path: Path) -> None:
    repo_dir = tmp_path / "sample_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    (repo_dir / "app.py").write_text(BASE_FILE, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True, capture_output=True)
    (repo_dir / "app.py").write_text(HEAD_FILE, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "change"], cwd=repo_dir, check=True, capture_output=True)

    db_path = tmp_path / "cli.db"
    out_json = tmp_path / "report.json"
    export_json = tmp_path / "export.json"
    runner = CliRunner()

    r_init = runner.invoke(app, ["init-db", "--db", str(db_path)])
    assert r_init.exit_code == 0

    r_rev = runner.invoke(
        app,
        [
            "review",
            str(repo_dir),
            "--mode",
            "A_STATIC",
            "--db",
            str(db_path),
            "--output",
            str(out_json),
            "--json-out",
        ],
    )
    assert r_rev.exit_code == 0
    rev_payload = json.loads(r_rev.stdout)
    assert rev_payload["run_id"] == 1
    assert len(rev_payload["findings"]) == 2
    assert out_json.exists()

    r_hist = runner.invoke(app, ["history", "--db", str(db_path), "--json-out"])
    assert r_hist.exit_code == 0
    hist_payload = json.loads(r_hist.stdout)
    assert len(hist_payload) == 1

    r_exp = runner.invoke(app, ["export", "1", "--db", str(db_path), "--output", str(export_json)])
    assert r_exp.exit_code == 0
    exp_payload = json.loads(export_json.read_text(encoding="utf-8"))
    assert len(exp_payload["findings"]) == 2

    r_purge = runner.invoke(app, ["purge", "--db", str(db_path), "--days-offset", "40"])
    assert r_purge.exit_code == 0
    purge_payload = json.loads(r_purge.stdout)
    assert purge_payload["runs_deleted"] == 1


def test_fix_verifier_and_missing_patch_insufficient_info() -> None:
    sqli_base = ReviewFinding(
        issue_fingerprint="fp-test-fix",
        file_path="app.py",
        diff_side=DiffSide.RIGHT,
        start_line=8,
        end_line=10,
        category=Category.SECURITY,
        finding_kind=FindingKind.DEFECT,
        severity=Severity.HIGH,
        evidence_status=EvidenceStatus.GROUNDED,
        title="SQLi",
        explanation="Dynamic SQL",
        code_evidence="query = f'SELECT *'",
        suggested_fix=None,
        sources=[SourceType.LLM],
    )
    assert verify_suggested_fix(sqli_base, {"app.py": HEAD_FILE}) == FixVerificationStatus.NOT_APPLICABLE

    clean_fix = sqli_base.model_copy(
        update={
            "suggested_fix": '```python\nconn.execute("SELECT * FROM users WHERE name = ?", (name,))\n```'
        }
    )
    assert verify_suggested_fix(clean_fix, {"app.py": HEAD_FILE}) == FixVerificationStatus.VERIFIED

    broken_fix = sqli_base.model_copy(update={"suggested_fix": "def broken(:"})
    assert verify_suggested_fix(broken_fix, {"app.py": HEAD_FILE}) == FixVerificationStatus.UNVERIFIED

    still_unsafe_fix = sqli_base.model_copy(
        update={"suggested_fix": "conn.execute(f'SELECT * FROM users WHERE name = \"{name}\"')"}
    )
    assert verify_suggested_fix(still_unsafe_fix, {"app.py": HEAD_FILE}) == FixVerificationStatus.UNVERIFIED

    missing_snap = CodeSnapshot(
        repo_full_name="org/huge-pr",
        input_source=InputSource.GITHUB_PR,
        base_commit_sha="a" * 40,
        head_commit_sha="b" * 40,
        merge_base_sha="a" * 40,
        diff_hash="c" * 64,
        total_files=1,
        total_additions=10,
        total_deletions=2,
        files=[
            ChangedFile(
                old_path="large.py",
                new_path="large.py",
                change_type=ChangeType.MODIFIED,
                patch_status=PatchStatus.MISSING_PATCH,
                language="Python",
                is_supported=True,
                hunks=[],
            )
        ],
    )
    missing_ingest = IngestResult(snapshot=missing_snap, parsed_files=[])
    rep_missing = ReviewOrchestrator().run(
        missing_ingest,
        ReviewRequest(repo_full_name="org/huge-pr", execution_mode=ExecutionMode.A_STATIC),
    )
    assert rep_missing.run_status == RunStatus.PARTIAL
    assert rep_missing.is_full_coverage is False
    assert all(
        qa.assessment_status == AssessmentStatus.INSUFFICIENT_INFO for qa in rep_missing.quality_assessments
    )
    assert all(qa.is_scope_complete is False for qa in rep_missing.quality_assessments)
