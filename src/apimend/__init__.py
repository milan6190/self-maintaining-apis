"""apimend: keep the APIs you consume from breaking you.

Detect contract drift, diagnose it, generate repair plans, and verify them.
"""

__version__ = "0.2.0"

from .diff import Change, diff_fields, diff_specs  # noqa: E402
from .heal import Healer, apply_plan, build_plan  # noqa: E402
from .schema import flatten, flatten_samples, infer  # noqa: E402
from .verify import validate, verify_plan  # noqa: E402

__all__ = [
    "__version__",
    "Change",
    "Healer",
    "apply_plan",
    "build_plan",
    "diff_fields",
    "diff_specs",
    "flatten",
    "flatten_samples",
    "infer",
    "validate",
    "verify_plan",
]
