"""Reusable experimental position-alignment API.

The package intentionally exposes only the BBV M1 implementation for now.
Semantic/DWARF adapters can consume the same plain result schema later without
changing the legacy checkpoint experiment.
"""

from .bbv import (
    AMBIGUOUS,
    ARTIFACT_MISMATCH,
    AlignmentPolicy,
    BbvEvent,
    CROSSING,
    EVIDENCE_COLLECTION_FAILED,
    INCOMPATIBLE_RUN,
    INVALID_SOURCE_ORDER,
    LOW_FIDELITY,
    MARKER_GAP_TOO_LARGE,
    NO_ANCHOR,
    NO_CANDIDATE,
    NONDETERMINISTIC_TRACE,
    OUT_OF_TRACE,
    POST_ALIGN_DIVERGENCE,
    POLICY_VERSION,
    PositionAligner,
    PositionIndex,
    RunSpec,
    SEARCH_TRUNCATED,
    SCHEMA_VERSION,
    align,
    build_index,
)


def policy_schema(policy: AlignmentPolicy | None = None) -> dict:
    """Return the versioned policy payload suitable for a JSON manifest."""

    return (policy or AlignmentPolicy()).as_dict()


def index_schema(index: PositionIndex) -> dict:
    """Return the serializable index metadata without profile vectors."""

    return index.to_dict()


def result_schema(result: dict) -> dict:
    """Validate/copy the public result envelope for protocol adapters.

    This hook deliberately performs only structural checks.  Semantic
    acceptance remains the responsibility of the calibrator/validator.
    """

    if not isinstance(result, dict):
        raise TypeError("alignment result must be a dict")
    required = {"schema_version", "policy_version", "status", "results"}
    missing = sorted(required - result.keys())
    if missing:
        raise ValueError(f"alignment result missing fields: {', '.join(missing)}")
    if result["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"unsupported alignment schema: {result['schema_version']!r}")
    if not isinstance(result["results"], dict):
        raise ValueError("alignment result 'results' must be an object")
    return result


__all__ = [
    "SCHEMA_VERSION",
    "POLICY_VERSION",
    "AlignmentPolicy",
    "BbvEvent",
    "RunSpec",
    "PositionIndex",
    "PositionAligner",
    "build_index",
    "align",
    "policy_schema",
    "index_schema",
    "result_schema",
    "NO_ANCHOR",
    "NO_CANDIDATE",
    "SEARCH_TRUNCATED",
    "AMBIGUOUS",
    "CROSSING",
    "MARKER_GAP_TOO_LARGE",
    "LOW_FIDELITY",
    "OUT_OF_TRACE",
    "POST_ALIGN_DIVERGENCE",
    "INVALID_SOURCE_ORDER",
    "INCOMPATIBLE_RUN",
    "NONDETERMINISTIC_TRACE",
    "ARTIFACT_MISMATCH",
    "EVIDENCE_COLLECTION_FAILED",
]
