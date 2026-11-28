from __future__ import annotations

from code_review_llm.domain import (
    AssessmentStatus,
    Category,
    CodeSnapshot,
    EvidenceStatus,
    FindingKind,
    PatchStatus,
    QualityAssessment,
    ReviewFinding,
    RunStatus,
)

ORDERED_DIMENSIONS: tuple[Category, ...] = (
    Category.FUNCTIONAL,
    Category.SECURITY,
    Category.PERFORMANCE,
    Category.MAINTAINABILITY,
    Category.CONVENTION,
)


def _dimension_has_tool_failure(dim: Category, tool_errors: list[str]) -> bool:
    for err in tool_errors:
        low = err.lower()
        if "ruff" in low:
            return True
        if "bandit" in low and dim == Category.SECURITY:
            return True
        if "ast" in low and dim in (
            Category.FUNCTIONAL,
            Category.SECURITY,
            Category.PERFORMANCE,
            Category.MAINTAINABILITY,
        ):
            return True
        if any(tok in low for tok in ("llm", "timeout", "external", "provider", "ceiling")):
            return True
    return False


def assess_quality(
    findings: list[ReviewFinding],
    snapshot: CodeSnapshot,
    run_status: RunStatus,
    is_full_coverage: bool,
    rubric_version: str = "rubric-v1",
    tool_errors: list[str] | None = None,
    omitted_files: list[str] | None = None,
    open_questions: list[str] | None = None,
) -> list[QualityAssessment]:
    errors = tool_errors or []
    omitted = omitted_files or []
    questions = open_questions or []

    has_supported_changes = any(
        cf.is_supported and cf.patch_status == PatchStatus.HAS_PATCH for cf in snapshot.files
    )
    has_missing_patch = any(
        cf.is_supported and cf.patch_status == PatchStatus.MISSING_PATCH for cf in snapshot.files
    )

    by_dim: dict[Category, list[ReviewFinding]] = {dim: [] for dim in ORDERED_DIMENSIONS}
    for fnd in findings:
        by_dim[fnd.category].append(fnd)

    assessments: list[QualityAssessment] = []
    for dim in ORDERED_DIMENSIONS:
        dim_findings = by_dim[dim]
        dim_tool_err = _dimension_has_tool_failure(dim, errors)
        dim_scope_complete = (
            is_full_coverage and run_status != RunStatus.FAILED and not dim_tool_err and not has_missing_patch
        )

        grounded = [f for f in dim_findings if f.evidence_status == EvidenceStatus.GROUNDED]
        needs_ver = [f for f in dim_findings if f.evidence_status == EvidenceStatus.NEEDS_VERIFICATION]

        if grounded:
            status = AssessmentStatus.ISSUES_FOUND
            bugs = sum(1 for f in grounded if f.finding_kind == FindingKind.DEFECT)
            opts = len(grounded) - bugs
            sev_parts = []
            for sev in ("HIGH", "MEDIUM", "LOW"):
                cnt = sum(1 for f in grounded if f.severity.value == sev)
                if cnt:
                    sev_parts.append(f"{cnt} {sev}")
            rationale = f"{bugs} potential defect(s), {opts} suggestion(s) ({', '.join(sev_parts)})."
            if not dim_scope_complete:
                rationale += " Scope incomplete due to partial coverage or tool errors."
        elif needs_ver:
            status = AssessmentStatus.NEEDS_VERIFICATION
            rationale = f"{len(needs_ver)} finding(s) require manual or caller verification."
            if not dim_scope_complete:
                rationale += " Scope incomplete."
        elif not has_supported_changes and not has_missing_patch:
            status = AssessmentStatus.NOT_APPLICABLE
            dim_scope_complete = not has_missing_patch
            if has_missing_patch:
                rationale = "Supported file patch content is missing from the snapshot."
            else:
                rationale = "No supported Python source changes in snapshot."
        elif not dim_scope_complete:
            status = AssessmentStatus.INSUFFICIENT_INFO
            reasons: list[str] = []
            if run_status == RunStatus.FAILED:
                reasons.append("review run failed")
            if dim_tool_err:
                reasons.append("analyzer or model execution error")
            if not is_full_coverage or omitted:
                reasons.append("truncated diff/AST context")
            if has_missing_patch:
                reasons.append("missing file patch")
            detail = ", ".join(reasons) if reasons else "incomplete review scope"
            rationale = f"Insufficient information to clear dimension: {detail}."
        else:
            status = AssessmentStatus.NO_ISSUES_OBSERVED
            rationale = "No grounded or unverified issues observed within complete scope."
            if questions and dim == Category.FUNCTIONAL:
                rationale += f" Open reviewer questions: {len(questions)}."

        assessments.append(
            QualityAssessment(
                dimension_name=dim,
                rubric_version=rubric_version,
                assessment_status=status,
                is_scope_complete=dim_scope_complete,
                summary_rationale=rationale,
            )
        )
    return assessments
