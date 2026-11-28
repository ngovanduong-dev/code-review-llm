from __future__ import annotations

import ast
import textwrap

from code_review_llm.analyzers.ast_probes import run_ast_probes_on_source
from code_review_llm.domain import DiffSide, FixVerificationStatus, ReviewFinding


def _strip_code_fence(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        s = "\n".join(lines)
    return s


def _leading_whitespace(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def verify_suggested_fix(finding: ReviewFinding, head_contents: dict[str, str]) -> FixVerificationStatus:
    if finding.suggested_fix is None or not finding.suggested_fix.strip():
        return FixVerificationStatus.NOT_APPLICABLE
    if finding.diff_side != DiffSide.RIGHT:
        return FixVerificationStatus.UNVERIFIED
    src = head_contents.get(finding.file_path)
    if not src or not finding.file_path.endswith((".py", ".pyi")):
        return FixVerificationStatus.UNVERIFIED

    orig_lines = src.splitlines()
    if finding.start_line < 1 or finding.end_line > len(orig_lines):
        return FixVerificationStatus.UNVERIFIED

    clean_fix = _strip_code_fence(finding.suggested_fix)
    if not clean_fix.strip():
        return FixVerificationStatus.UNVERIFIED

    base_indent = _leading_whitespace(orig_lines[finding.start_line - 1])
    dedented_lines = textwrap.dedent(clean_fix).splitlines()
    if not dedented_lines:
        return FixVerificationStatus.UNVERIFIED
    reindented = [f"{base_indent}{ln}" if ln.strip() else "" for ln in dedented_lines]

    patched_lines = orig_lines[: finding.start_line - 1] + reindented + orig_lines[finding.end_line :]
    patched = "\n".join(patched_lines) + ("\n" if src.endswith("\n") else "")

    try:
        ast.parse(src, filename=finding.file_path)
        ast.parse(patched, filename=finding.file_path)
    except SyntaxError:
        return FixVerificationStatus.UNVERIFIED

    orig_probes = run_ast_probes_on_source(finding.file_path, src)
    patched_probes = run_ast_probes_on_source(finding.file_path, patched)
    if orig_probes.errors or patched_probes.errors:
        return FixVerificationStatus.UNVERIFIED
    if len(patched_probes.candidates) > len(orig_probes.candidates):
        return FixVerificationStatus.UNVERIFIED

    rep_start = finding.start_line
    rep_end = finding.start_line + len(reindented) - 1
    for cand in patched_probes.candidates:
        if cand.start_line <= rep_end and cand.end_line >= rep_start:
            return FixVerificationStatus.UNVERIFIED

    return FixVerificationStatus.VERIFIED


def apply_fix_verification(
    findings: list[ReviewFinding],
    head_contents: dict[str, str],
) -> list[ReviewFinding]:
    updated: list[ReviewFinding] = []
    for fnd in findings:
        status = verify_suggested_fix(fnd, head_contents)
        updated.append(fnd.model_copy(update={"fix_verification_status": status}))
    return updated
