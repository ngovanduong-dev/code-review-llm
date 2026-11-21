from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from code_review_llm.domain import (
    CallStatus,
    Category,
    ChangedFile,
    ChangeType,
    DiffHunk,
    DiffSide,
    EvidenceStatus,
    FeedbackLabel,
    FindingCandidate,
    FindingFeedback,
    FindingKind,
    ModelInvocationRecord,
    ReviewFinding,
    Severity,
    SourceType,
    UsageSource,
)


def test_strict_int_rejects_bool() -> None:
    with pytest.raises(ValidationError):
        DiffHunk(
            hunk_index=True,
            old_start_line=1,
            old_line_span=2,
            new_start_line=1,
            new_line_span=3,
        )


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        DiffHunk(
            hunk_index=1,
            old_start_line=1,
            old_line_span=2,
            new_start_line=1,
            new_line_span=3,
            unexpected="boom",
        )


def test_changed_file_path_rules() -> None:
    f_add = ChangedFile(old_path=None, new_path="a.py", change_type=ChangeType.ADDED)
    assert f_add.display_path == "a.py"
    with pytest.raises(ValidationError):
        ChangedFile(old_path="old.py", new_path="a.py", change_type=ChangeType.ADDED)
    with pytest.raises(ValidationError):
        ChangedFile(old_path="old.py", new_path="a.py", change_type=ChangeType.DELETED)


def test_finding_line_bounds_and_roundtrip() -> None:
    with pytest.raises(ValidationError):
        ReviewFinding(
            issue_fingerprint="fp1",
            file_path="a.py",
            diff_side=DiffSide.RIGHT,
            start_line=10,
            end_line=8,
            category=Category.FUNCTIONAL,
            finding_kind=FindingKind.DEFECT,
            severity=Severity.HIGH,
            evidence_status=EvidenceStatus.GROUNDED,
            title="t",
            explanation="e",
            code_evidence="x = 1",
            sources=[SourceType.LLM],
        )
    f = ReviewFinding(
        issue_fingerprint="fp1",
        file_path="a.py",
        diff_side=DiffSide.RIGHT,
        start_line=8,
        end_line=10,
        category=Category.FUNCTIONAL,
        finding_kind=FindingKind.DEFECT,
        severity=Severity.HIGH,
        evidence_status=EvidenceStatus.GROUNDED,
        title="t",
        explanation="e",
        code_evidence="x = 1",
        sources=[SourceType.RUFF, SourceType.LLM],
    )
    raw = f.model_dump_json()
    parsed = ReviewFinding.model_validate_json(raw)
    assert parsed == f


def test_candidate_source_xor_constraint() -> None:
    with pytest.raises(ValidationError):
        FindingCandidate(
            file_path="a.py",
            source_type=SourceType.LLM,
            diff_side=DiffSide.RIGHT,
            start_line=1,
            end_line=2,
            category=Category.SECURITY,
            severity=Severity.HIGH,
            title="t",
            raw_message="m",
            rule_code="B608",
            invocation_attempt=1,
        )
    c = FindingCandidate(
        file_path="a.py",
        source_type=SourceType.RUFF,
        diff_side=DiffSide.RIGHT,
        start_line=1,
        end_line=2,
        category=Category.FUNCTIONAL,
        severity=Severity.MEDIUM,
        title="mutable default",
        raw_message="B006",
        rule_code="B006",
    )
    assert c.rule_code == "B006"


def test_usage_null_consistency() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        ModelInvocationRecord(
            attempt_number=1,
            provider_name="gemini",
            resolved_model_name="gemini-2.5-flash",
            payload_hash="h",
            call_status=CallStatus.SUCCESS,
            input_tokens=100,
            output_tokens=None,
            usage_source=UsageSource.NONE,
            started_at=now,
        )
    rec = ModelInvocationRecord(
        attempt_number=1,
        provider_name="gemini",
        resolved_model_name="gemini-2.5-flash",
        payload_hash="h",
        call_status=CallStatus.TIMEOUT,
        usage_source=UsageSource.NONE,
        started_at=now,
    )
    assert rec.input_tokens is None


def test_feedback_sai_requires_reason() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        FindingFeedback(
            finding_id=1,
            user_id=1,
            revision_no=1,
            feedback_label=FeedbackLabel.INVALID,
            reason_note="   ",
            created_at=now,
        )
    fb = FindingFeedback(
        finding_id=1,
        user_id=1,
        revision_no=1,
        feedback_label=FeedbackLabel.INVALID,
        reason_note="Guarded above",
        created_at=now,
    )
    assert fb.feedback_label == FeedbackLabel.INVALID
