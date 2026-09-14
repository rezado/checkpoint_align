"""Public interface for dynamic cross-ELF progress alignment.

Callers provide complete, build-bound dynamic event envelopes and receive an
explicit ``Correspondence``.  Static catalogs and runner adapters remain
internal inputs; the removed BBV/interval locator is not part of this package.
"""

from .align import PositionAligner
from .source_boundary import BoundaryPolicy, CheckpointPoint, NemuSourceProbeRunner, SourceBinding, SourceBindingBatch, SourceBoundaryResolver
from .protocol import (
    AlignmentResult,
    BuildRun,
    Correspondence,
    Position,
    ProgressEvent,
    SCHEMA_VERSION,
    validate_events,
    validate_position,
    validate_run,
)

__all__ = [
    "SCHEMA_VERSION",
    "BuildRun",
    "ProgressEvent",
    "Position",
    "Correspondence",
    "AlignmentResult",
    "PositionAligner",
    "BoundaryPolicy",
    "CheckpointPoint",
    "NemuSourceProbeRunner",
    "SourceBinding",
    "SourceBindingBatch",
    "SourceBoundaryResolver",
    "validate_run",
    "validate_events",
    "validate_position",
]
