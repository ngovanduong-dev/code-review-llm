from enum import StrEnum


class AccountType(StrEnum):
    PERSONAL = "PERSONAL"
    CI_SERVICE = "CI_SERVICE"


class AccountStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class Platform(StrEnum):
    GITHUB = "GITHUB"
    LOCAL = "LOCAL"


class RepoStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class RoleName(StrEnum):
    DEVELOPER = "DEVELOPER"
    REVIEWER = "REVIEWER"
    PROJECT_MANAGER = "PROJECT_MANAGER"
    EVALUATOR = "EVALUATOR"


class PRStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    MERGED = "MERGED"


class WebhookProcessStatus(StrEnum):
    RECEIVED = "RECEIVED"
    RUN_CREATED = "RUN_CREATED"
    IGNORED = "IGNORED"
    ERROR = "ERROR"


class InputSource(StrEnum):
    GITHUB_PR = "GITHUB_PR"
    LOCAL_CLI = "LOCAL_CLI"
    IDE_PLUGIN = "IDE_PLUGIN"


class ChangeType(StrEnum):
    ADDED = "ADDED"
    MODIFIED = "MODIFIED"
    DELETED = "DELETED"
    RENAMED = "RENAMED"


class PatchStatus(StrEnum):
    HAS_PATCH = "HAS_PATCH"
    MISSING_PATCH = "MISSING_PATCH"
    BINARY = "BINARY"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class ToolSource(StrEnum):
    RUFF = "RUFF"
    BANDIT = "BANDIT"
    AST_CUSTOM = "AST_CUSTOM"
    CONVENTION = "CONVENTION"


class SourceType(StrEnum):
    RUFF = "RUFF"
    BANDIT = "BANDIT"
    AST_CUSTOM = "AST_CUSTOM"
    CONVENTION = "CONVENTION"
    LLM = "LLM"


class Category(StrEnum):
    FUNCTIONAL = "FUNCTIONAL"
    SECURITY = "SECURITY"
    PERFORMANCE = "PERFORMANCE"
    MAINTAINABILITY = "MAINTAINABILITY"
    CONVENTION = "CONVENTION"


class Severity(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ExecutionMode(StrEnum):
    A_STATIC = "A_STATIC"
    B_LLM = "B_LLM"
    C_CONCAT = "C_CONCAT"
    D_HYBRID = "D_HYBRID"


class ContextPolicy(StrEnum):
    DIFF_ONLY = "DIFF_ONLY"
    DIFF_AST = "DIFF_AST"
    DIFF_AST_INTENT = "DIFF_AST_INTENT"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class RunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class CallPurpose(StrEnum):
    REVIEW = "REVIEW"
    VERIFY = "VERIFY"
    RETRY = "RETRY"


class CallStatus(StrEnum):
    SUCCESS = "SUCCESS"
    TIMEOUT = "TIMEOUT"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    SCHEMA_ERROR = "SCHEMA_ERROR"


class UsageSource(StrEnum):
    ACTUAL = "ACTUAL"
    ESTIMATED = "ESTIMATED"
    NONE = "NONE"


class AssessmentStatus(StrEnum):
    ISSUES_FOUND = "ISSUES_FOUND"
    NEEDS_VERIFICATION = "NEEDS_VERIFICATION"
    NO_ISSUES_OBSERVED = "NO_ISSUES_OBSERVED"
    INSUFFICIENT_INFO = "INSUFFICIENT_INFO"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class DiffSide(StrEnum):
    LEFT = "LEFT"
    RIGHT = "RIGHT"


class FindingKind(StrEnum):
    DEFECT = "DEFECT"
    SUGGESTION = "SUGGESTION"


class EvidenceStatus(StrEnum):
    GROUNDED = "GROUNDED"
    NEEDS_VERIFICATION = "NEEDS_VERIFICATION"


class FixVerificationStatus(StrEnum):
    UNVERIFIED = "UNVERIFIED"
    VERIFIED = "VERIFIED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class FilterStatus(StrEnum):
    KEPT = "KEPT"
    DROPPED_INVALID_LINE = "DROPPED_INVALID_LINE"
    DROPPED_DUPLICATE = "DROPPED_DUPLICATE"
    DROPPED_UNGROUNDED = "DROPPED_UNGROUNDED"
    PRE_EXISTING = "PRE_EXISTING"


class FeedbackLabel(StrEnum):
    VALID = "VALID"
    INVALID = "INVALID"
    NEEDS_VERIFICATION = "NEEDS_VERIFICATION"


class CommentScope(StrEnum):
    PR_SUMMARY = "PR_SUMMARY"
    INLINE_COMMENT = "INLINE_COMMENT"


class PublishStatus(StrEnum):
    PENDING = "PENDING"
    PUBLISHED = "PUBLISHED"
    SKIPPED_STALE = "SKIPPED_STALE"
    FAILED = "FAILED"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"


class ArtifactType(StrEnum):
    RAW_SNAPSHOT = "RAW_SNAPSHOT"
    RAW_PROMPT = "RAW_PROMPT"
    RAW_LLM_OUTPUT = "RAW_LLM_OUTPUT"
    REPORT_JSON = "REPORT_JSON"
    REPORT_MD = "REPORT_MD"
    REPORT_SARIF = "REPORT_SARIF"


class PurgeStatus(StrEnum):
    STORED = "STORED"
    PURGED = "PURGED"
    DELETE_FAILED = "DELETE_FAILED"
