from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from code_review_llm.analyzers import DifferentialStaticEngine
from code_review_llm.context import ContextBundle, estimate_tokens, pack_context_budget
from code_review_llm.db import ReviewStore
from code_review_llm.domain import (
    CallPurpose,
    CallStatus,
    ChangedFile,
    CodeSnapshot,
    ContextPolicy,
    DiffHunk,
    EvidenceStatus,
    ExecutionMode,
    FilterStatus,
    FindingCandidate,
    FixVerificationStatus,
    InputSource,
    ModelInvocationRecord,
    PatchStatus,
    ReviewFinding,
    ReviewReport,
    ReviewRequest,
    RunStatus,
    Severity,
)
from code_review_llm.ingest import IngestResult
from code_review_llm.llm import LLMProvider, LLMReviewOutput, RateLimitedRouter
from code_review_llm.pipeline.fix_verifier import apply_fix_verification
from code_review_llm.pipeline.grounding import (
    CandidateEnvelope,
    DeterministicGroundingGate,
    llm_items_to_envelopes,
)
from code_review_llm.pipeline.merger import SemanticMerger
from code_review_llm.pipeline.quality import assess_quality
from code_review_llm.pipeline.verifier import SelectiveVerificationFilter
from code_review_llm.prompts import PromptSpec, load_prompt_spec, render_review_prompt

_LOCAL_PROVIDER_NAMES = frozenset({"replay", "ollama", "vllm"})
_DEFAULT_MODELS = {
    "replay": "replay-v1",
    "gemini": "gemini-2.5-flash",
    "groq": "llama-3.3-70b-versatile",
    "openrouter": "meta-llama/llama-3.3-70b-instruct:free",
    "cerebras": "llama-3.3-70b",
}


@dataclass
class OrchestratorConfig:
    enable_verification: bool = True
    check_diff_scope: bool = True
    check_ast_symbols: bool = True
    rpm_limit: int = 12
    max_retries: int = 2
    disabled_rules: set[str] | None = None
    severity_overrides: dict[str, Severity] | None = None
    review_prompt_name: str = "py_review_v1"
    verify_prompt_name: str = "py_verify_v1"


def _build_idempotency_key(
    ingest: IngestResult,
    request: ReviewRequest,
    effective_context_policy: ContextPolicy,
    resolved_model: str | None,
    prompt_hash: str | None,
) -> str:
    payload = {
        "repo": ingest.snapshot.repo_full_name,
        "base_sha": ingest.snapshot.base_commit_sha,
        "head_sha": ingest.snapshot.head_commit_sha,
        "merge_base_sha": ingest.snapshot.merge_base_sha,
        "diff_hash": ingest.snapshot.diff_hash,
        "mode": request.execution_mode.value,
        "context_policy": effective_context_policy.value,
        "config_version": request.config_version,
        "model": resolved_model,
        "prompt_hash": prompt_hash,
        "policy": request.policy.model_dump(mode="json"),
        "benchmark_sample_id": request.benchmark_sample_id,
        "benchmark_manifest_hash": request.benchmark_manifest_hash,
    }
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _estimate_static_signals_tokens(candidates: list[FindingCandidate]) -> int:
    if not candidates:
        return 0
    lines = [
        f"- {c.file_path}:L{c.start_line}-{c.end_line} [{c.source_type.value}/{c.rule_code}] {c.raw_message}"
        for c in candidates
    ]
    block = "<static_signals>\n" + "\n".join(lines) + "\n</static_signals>\n"
    return estimate_tokens(block)


def _concat_without_merge(
    envelopes: list[CandidateEnvelope],
) -> tuple[list[ReviewFinding], list[FindingCandidate]]:
    findings: list[ReviewFinding] = []
    updated_cands: list[FindingCandidate] = []
    used_fps: set[str] = set()

    for idx, env in enumerate(envelopes):
        c = env.candidate
        seed = (
            f"concat|{idx}|{c.source_type.value}|{c.rule_code or ''}|"
            f"{c.file_path}|{c.diff_side.value}|{c.start_line}|{c.end_line}|{c.title}"
        )
        fp = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:40]
        if fp in used_fps:
            fp = hashlib.sha256(f"{seed}|{len(used_fps)}".encode()).hexdigest()[:40]
        used_fps.add(fp)
        findings.append(
            ReviewFinding(
                issue_fingerprint=fp,
                file_path=c.file_path,
                diff_side=c.diff_side,
                start_line=c.start_line,
                end_line=c.end_line,
                hunk_index=env.hunk_index,
                category=c.category,
                finding_kind=c.finding_kind,
                severity=c.severity,
                evidence_status=env.evidence_status,
                title=c.title[:300],
                explanation=c.raw_message,
                code_evidence=c.code_evidence or c.raw_message[:200],
                suggested_fix=c.suggested_fix,
                fix_verification_status=(
                    FixVerificationStatus.UNVERIFIED
                    if c.suggested_fix
                    else FixVerificationStatus.NOT_APPLICABLE
                ),
                sources=[c.source_type],
            )
        )
        updated_cands.append(
            c.model_copy(update={"filter_status": FilterStatus.KEPT, "merged_fingerprint": fp})
        )
    return findings, updated_cands


def _enrich_snapshot_with_bundle(snapshot: CodeSnapshot, bundle: ContextBundle) -> CodeSnapshot:
    chunk_map = {(ch.file_path, ch.hunk_index): ch for ch in bundle.chunks}
    new_files: list[ChangedFile] = []
    for cf in snapshot.files:
        new_hunks: list[DiffHunk] = []
        for hk in cf.hunks:
            ch = chunk_map.get((cf.display_path, hk.hunk_index))
            if ch is not None:
                new_hunks.append(
                    hk.model_copy(
                        update={
                            "ast_symbol_name": ch.ast_symbol_name or hk.ast_symbol_name,
                            "ast_context_snippet": ch.ast_context_text,
                            "estimated_tokens": ch.estimated_tokens,
                        }
                    )
                )
            else:
                new_hunks.append(hk)
        new_files.append(cf.model_copy(update={"hunks": new_hunks}))
    return snapshot.model_copy(update={"files": new_files})


class ReviewOrchestrator:
    def __init__(
        self,
        static_engine: DifferentialStaticEngine | None = None,
        provider: LLMProvider | None = None,
        config: OrchestratorConfig | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.static_engine = static_engine
        self.provider = provider
        self.config = config or OrchestratorConfig()
        self.sleep_fn = sleep_fn
        self.clock_fn = clock_fn

    def _effective_static_engine(self) -> DifferentialStaticEngine:
        if self.static_engine is not None:
            return self.static_engine
        return DifferentialStaticEngine(
            disabled_rules=self.config.disabled_rules,
            severity_overrides=self.config.severity_overrides,
        )

    def _resolve_model_name(self, request: ReviewRequest) -> str | None:
        if request.execution_mode == ExecutionMode.A_STATIC:
            return None
        if request.llm_model_name:
            return request.llm_model_name
        if self.provider is not None:
            return _DEFAULT_MODELS.get(self.provider.provider_name, "replay-v1")
        return None

    def run(self, ingest: IngestResult, request: ReviewRequest) -> ReviewReport:
        created_at = datetime.now(UTC)
        started_at = datetime.now(UTC)
        deadline = self.clock_fn() + float(request.policy.timeout_seconds)

        mode = request.execution_mode
        eff_policy = (
            ContextPolicy.NOT_APPLICABLE if mode == ExecutionMode.A_STATIC else request.context_policy
        )
        resolved_model = self._resolve_model_name(request)

        review_spec: PromptSpec | None = None
        verify_spec: PromptSpec | None = None
        if mode != ExecutionMode.A_STATIC:
            review_name = request.prompt_code or self.config.review_prompt_name
            review_spec = load_prompt_spec(review_name)
            if mode in (ExecutionMode.B_LLM, ExecutionMode.D_HYBRID) and self.config.enable_verification:
                verify_spec = load_prompt_spec(self.config.verify_prompt_name)

        idem_key = _build_idempotency_key(
            ingest=ingest,
            request=request,
            effective_context_policy=eff_policy,
            resolved_model=resolved_model,
            prompt_hash=review_spec.prompt_hash if review_spec else None,
        )

        if ingest.is_empty:
            finished_at = datetime.now(UTC)
            has_missing_patch = any(
                cf.is_supported and cf.patch_status == PatchStatus.MISSING_PATCH
                for cf in ingest.snapshot.files
            )
            empty_status = RunStatus.PARTIAL if has_missing_patch else RunStatus.COMPLETED
            empty_cov = not has_missing_patch
            empty_term = "Supported file patch missing from snapshot" if has_missing_patch else None
            empty_warnings = [f"Skipped non-Python/unsupported file: {p}" for p in ingest.skipped_files]
            if has_missing_patch:
                empty_warnings.append("Supported file patch missing from snapshot")
            qas = assess_quality(
                findings=[],
                snapshot=ingest.snapshot,
                run_status=empty_status,
                is_full_coverage=empty_cov,
                rubric_version=request.policy.rubric_version,
            )
            return ReviewReport(
                idempotency_key=idem_key,
                snapshot=ingest.snapshot,
                execution_mode=mode,
                context_policy=eff_policy,
                run_status=empty_status,
                is_full_coverage=empty_cov,
                termination_reason=empty_term,
                quality_assessments=qas,
                warnings=empty_warnings,
                benchmark_sample_id=request.benchmark_sample_id,
                benchmark_manifest_hash=request.benchmark_manifest_hash,
                created_at=created_at,
                started_at=started_at,
                finished_at=finished_at,
            )

        if mode != ExecutionMode.A_STATIC:
            block_reason: str | None = None
            if self.provider is None:
                block_reason = "No LLM provider configured for LLM-enabled mode"
            elif (
                self.provider.provider_name not in _LOCAL_PROVIDER_NAMES
                and not request.policy.allow_external_llm
            ):
                block_reason = "External LLM calls blocked by allow_external_llm=False"
            if block_reason is not None:
                finished_at = datetime.now(UTC)
                qas = assess_quality(
                    findings=[],
                    snapshot=ingest.snapshot,
                    run_status=RunStatus.FAILED,
                    is_full_coverage=False,
                    rubric_version=request.policy.rubric_version,
                    tool_errors=[block_reason],
                )
                return ReviewReport(
                    idempotency_key=idem_key,
                    snapshot=ingest.snapshot,
                    execution_mode=mode,
                    context_policy=eff_policy,
                    run_status=RunStatus.FAILED,
                    is_full_coverage=False,
                    termination_reason=block_reason,
                    quality_assessments=qas,
                    warnings=[block_reason],
                    benchmark_sample_id=request.benchmark_sample_id,
                    benchmark_manifest_hash=request.benchmark_manifest_hash,
                    created_at=created_at,
                    started_at=started_at,
                    finished_at=finished_at,
                )

        gate = DeterministicGroundingGate(
            check_diff_scope=self.config.check_diff_scope,
            check_ast_symbols=self.config.check_ast_symbols,
        )
        warnings: list[str] = [f"Skipped unsupported file: {p}" for p in ingest.skipped_files]
        all_candidates: list[FindingCandidate] = []
        tool_errors: list[str] = []
        invocations: list[ModelInvocationRecord] = []
        open_questions: list[str] = []

        static_kept: list[CandidateEnvelope] = []
        static_ran = mode in (ExecutionMode.A_STATIC, ExecutionMode.C_CONCAT, ExecutionMode.D_HYBRID)
        if static_ran:
            static_out = self._effective_static_engine().analyze(
                ingest.parsed_files,
                ingest.base_contents,
                ingest.head_contents,
            )
            tool_errors.extend(static_out.errors)
            warnings.extend(static_out.errors)
            active_static = []
            for cand in static_out.candidates:
                if cand.filter_status == FilterStatus.PRE_EXISTING:
                    all_candidates.append(cand)
                else:
                    active_static.append(
                        CandidateEnvelope(candidate=cand, evidence_status=EvidenceStatus.GROUNDED)
                    )
            s_ground = gate.verify(
                envelopes=active_static,
                snapshot=ingest.snapshot,
                parsed_files=ingest.parsed_files,
                base_contents=ingest.base_contents,
                head_contents=ingest.head_contents,
            )
            static_kept = s_ground.kept
            all_candidates.extend(s_ground.rejected)
            warnings.extend(s_ground.warnings)

        has_missing_patch = any(
            cf.is_supported and cf.patch_status == PatchStatus.MISSING_PATCH for cf in ingest.snapshot.files
        )
        if mode == ExecutionMode.A_STATIC:
            merged = SemanticMerger().merge(static_kept)
            static_findings = apply_fix_verification(merged.findings, ingest.head_contents)
            all_candidates.extend(merged.updated_candidates)
            full_cov = len(tool_errors) == 0 and not has_missing_patch
            status = RunStatus.COMPLETED if full_cov else RunStatus.PARTIAL
            term = (
                tool_errors[0][:500]
                if tool_errors
                else ("Supported file patch missing from snapshot" if has_missing_patch else None)
            )
            qas = assess_quality(
                findings=static_findings,
                snapshot=ingest.snapshot,
                run_status=status,
                is_full_coverage=full_cov,
                rubric_version=request.policy.rubric_version,
                tool_errors=tool_errors,
                open_questions=[],
            )
            return ReviewReport(
                idempotency_key=idem_key,
                snapshot=ingest.snapshot,
                execution_mode=mode,
                context_policy=eff_policy,
                run_status=status,
                is_full_coverage=full_cov,
                termination_reason=term,
                findings=static_findings,
                candidates=all_candidates,
                quality_assessments=qas,
                invocations=[],
                omitted_files=[],
                warnings=warnings,
                benchmark_sample_id=request.benchmark_sample_id,
                benchmark_manifest_hash=request.benchmark_manifest_hash,
                created_at=created_at,
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

        static_signals_for_prompt = (
            [e.candidate for e in static_kept] if mode == ExecutionMode.D_HYBRID else []
        )
        static_sig_tokens = _estimate_static_signals_tokens(static_signals_for_prompt)
        bundle: ContextBundle = pack_context_budget(
            parsed_files=ingest.parsed_files,
            base_contents=ingest.base_contents,
            head_contents=ingest.head_contents,
            context_policy=eff_policy,
            policy=request.policy,
            pr_title=ingest.snapshot.pr_title,
            pr_description=ingest.snapshot.pr_description,
            static_signals_tokens=static_sig_tokens,
        )
        enriched_snapshot = _enrich_snapshot_with_bundle(ingest.snapshot, bundle)
        warnings.extend(bundle.warnings)

        assert self.provider is not None
        assert review_spec is not None
        assert resolved_model is not None

        router = RateLimitedRouter(
            provider=self.provider,
            rpm_limit=self.config.rpm_limit,
            max_retries=self.config.max_retries,
            sleep_fn=self.sleep_fn,
            clock_fn=self.clock_fn,
        )
        attempt_counter = [0]
        llm_kept: list[CandidateEnvelope] = []
        llm_failed_msg: str | None = None

        if not bundle.chunks:
            llm_failed_msg = "No diff chunks fit within max_input_tokens budget"
            warnings.append(llm_failed_msg)
            tool_errors.append(llm_failed_msg)
        elif self.clock_fn() >= deadline:
            llm_failed_msg = "Timeout ceiling reached before LLM review call"
            warnings.append(llm_failed_msg)
            tool_errors.append(f"timeout: {llm_failed_msg}")
        elif request.policy.max_llm_calls <= 0:
            llm_failed_msg = "LLM review blocked by max_llm_calls=0 ceiling"
            warnings.append(llm_failed_msg)
            tool_errors.append(f"ceiling: {llm_failed_msg}")
        else:
            remaining_sec = max(1, int(deadline - self.clock_fn()))
            user_prompt = render_review_prompt(
                review_spec,
                bundle,
                static_candidates=static_signals_for_prompt or None,
            )
            resp, rev_records = router.invoke_structured(
                system_prompt=review_spec.system_prompt,
                user_prompt=user_prompt,
                schema_cls=LLMReviewOutput,
                model_name=resolved_model,
                temperature=request.policy.temperature,
                max_output_tokens=request.policy.max_output_tokens,
                timeout_seconds=min(remaining_sec, request.policy.timeout_seconds),
                call_purpose=CallPurpose.REVIEW,
                attempt_counter=attempt_counter,
                max_total_calls=request.policy.max_llm_calls,
            )
            invocations.extend(rev_records)
            if resp is None or resp.call_status != CallStatus.SUCCESS or resp.parsed is None:
                detail = resp.error_detail if resp and resp.error_detail else "provider failure"
                status_lbl = resp.call_status.value if resp else "CEILING"
                llm_failed_msg = f"LLM review failed ({status_lbl}): {detail}"
                warnings.append(llm_failed_msg)
                tool_errors.append(llm_failed_msg)
            else:
                review_out = (
                    resp.parsed
                    if isinstance(resp.parsed, LLMReviewOutput)
                    else LLMReviewOutput.model_validate(resp.parsed.model_dump())
                )
                open_questions.extend(review_out.open_questions)
                rev_attempt = rev_records[-1].attempt_number
                raw_envelopes, invalid_orders = llm_items_to_envelopes(
                    review_out.findings,
                    invocation_attempt=rev_attempt,
                )
                l_ground = gate.verify(
                    envelopes=raw_envelopes,
                    snapshot=ingest.snapshot,
                    parsed_files=ingest.parsed_files,
                    base_contents=ingest.base_contents,
                    head_contents=ingest.head_contents,
                    invalid_order_indices=invalid_orders,
                )
                llm_kept = l_ground.kept
                all_candidates.extend(l_ground.rejected)
                warnings.extend(l_ground.warnings)

        final_findings: list[ReviewFinding] = []
        if mode == ExecutionMode.C_CONCAT:
            concat_envelopes = static_kept + llm_kept
            final_findings, concat_cands = _concat_without_merge(concat_envelopes)
            all_candidates.extend(concat_cands)
        else:
            combined_envelopes = llm_kept if mode == ExecutionMode.B_LLM else (static_kept + llm_kept)
            should_verify = (
                self.config.enable_verification and len(combined_envelopes) > 0 and llm_failed_msg is None
            )
            if should_verify:
                if self.clock_fn() >= deadline:
                    msg = "Skipped verification filter due to timeout ceiling"
                    warnings.append(msg)
                    tool_errors.append(f"timeout: {msg}")
                else:
                    rem_sec = max(1, int(deadline - self.clock_fn()))
                    verifier = SelectiveVerificationFilter(
                        router=router,
                        enabled=True,
                        model_name=resolved_model,
                        temperature=request.policy.temperature,
                        max_output_tokens=min(2048, request.policy.max_output_tokens),
                        timeout_seconds=min(rem_sec, request.policy.timeout_seconds),
                        prompt_spec=verify_spec,
                        max_total_calls=request.policy.max_llm_calls,
                    )
                    v_res = verifier.filter(combined_envelopes, bundle, attempt_counter=attempt_counter)
                    invocations.extend(v_res.invocations)
                    all_candidates.extend(v_res.rejected)
                    warnings.extend(v_res.warnings)
                    if v_res.warnings:
                        tool_errors.extend(v_res.warnings)
                    combined_envelopes = v_res.kept

            merged = SemanticMerger().merge(combined_envelopes)
            final_findings = merged.findings
            all_candidates.extend(merged.updated_candidates)

        final_findings = apply_fix_verification(final_findings, ingest.head_contents)

        if mode == ExecutionMode.B_LLM and llm_failed_msg is not None:
            run_status = RunStatus.FAILED
            full_cov = False
            term = llm_failed_msg[:500]
        elif llm_failed_msg is not None or tool_errors or not bundle.is_full_coverage or has_missing_patch:
            run_status = RunStatus.PARTIAL
            full_cov = False
            term = (
                llm_failed_msg
                or (tool_errors[0] if tool_errors else None)
                or ("Supported file patch missing from snapshot" if has_missing_patch else "Partial context")
            )[:500]
        else:
            run_status = RunStatus.COMPLETED
            full_cov = True
            term = None

        qas = assess_quality(
            findings=final_findings,
            snapshot=enriched_snapshot,
            run_status=run_status,
            is_full_coverage=full_cov,
            rubric_version=request.policy.rubric_version,
            tool_errors=tool_errors,
            omitted_files=bundle.omitted_files,
            open_questions=open_questions,
        )
        return ReviewReport(
            idempotency_key=idem_key,
            snapshot=enriched_snapshot,
            execution_mode=mode,
            context_policy=eff_policy,
            run_status=run_status,
            is_full_coverage=full_cov,
            termination_reason=term,
            findings=final_findings,
            candidates=all_candidates,
            quality_assessments=qas,
            invocations=invocations,
            omitted_files=bundle.omitted_files,
            warnings=warnings,
            benchmark_sample_id=request.benchmark_sample_id,
            benchmark_manifest_hash=request.benchmark_manifest_hash,
            created_at=created_at,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )


def save_review_run(
    store: ReviewStore,
    request: ReviewRequest,
    report: ReviewReport,
    prompt_spec: PromptSpec | None = None,
    username: str = "cli_user",
    event_id: int | None = None,
    pr_source_branch: str = "head",
    pr_target_branch: str = "main",
    pr_status: str = "OPEN",
) -> ReviewReport:
    account_type = "CI_SERVICE" if report.snapshot.input_source == InputSource.GITHUB_PR else "PERSONAL"
    user_id = store.ensure_user(username, username, account_type=account_type)
    platform = "GITHUB" if report.snapshot.input_source == InputSource.GITHUB_PR else "LOCAL"
    repo_id = store.ensure_repo(report.snapshot.repo_full_name, platform=platform)

    pr_id: int | None = None
    if report.snapshot.pr_number is not None:
        pr_id = store.upsert_pull_request(
            repo_id=repo_id,
            pr_number=report.snapshot.pr_number,
            title=report.snapshot.pr_title or f"PR #{report.snapshot.pr_number}",
            source_branch=pr_source_branch,
            target_branch=pr_target_branch,
            current_head_sha=report.snapshot.head_commit_sha,
            pr_status=pr_status,
            description=report.snapshot.pr_description,
            author_id=user_id,
        )

    eff_model: str | None = None
    prompt_id: int | None = None
    if report.execution_mode != ExecutionMode.A_STATIC:
        spec = prompt_spec or load_prompt_spec(request.prompt_code or "py_review_v1")
        prompt_id = store.ensure_prompt_template(
            prompt_code=spec.prompt_code,
            version_tag=spec.version_tag,
            strategy_desc=spec.strategy_desc,
            system_prompt_text=spec.system_prompt,
            user_prompt_template=spec.user_template,
            output_json_schema=json.dumps(LLMReviewOutput.model_json_schema(), sort_keys=True),
        )
        eff_model = request.llm_model_name or (
            report.invocations[0].resolved_model_name if report.invocations else None
        )

    eff_request = request.model_copy(
        update={
            "context_policy": report.context_policy,
            "llm_model_name": eff_model,
        }
    )
    config_id = store.ensure_review_config(repo_id=repo_id, request=eff_request, prompt_id=prompt_id)
    snapshot_id, file_map, hunk_map = store.save_snapshot(
        repo_id=repo_id,
        snapshot=report.snapshot,
        pr_id=pr_id,
    )
    run_id, created = store.create_run(
        snapshot_id=snapshot_id,
        config_id=config_id,
        request=eff_request,
        idempotency_key=report.idempotency_key,
        requested_by=user_id,
        event_id=event_id,
    )
    if created:
        store.persist_report(
            run_id=run_id,
            snapshot_id=snapshot_id,
            file_map=file_map,
            hunk_map=hunk_map,
            report=report,
        )
    return report.model_copy(update={"run_id": run_id})
