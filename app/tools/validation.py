"""JSON Schema validation of the arguments a model produced for a tool.

Models hallucinate argument shapes routinely, so this runs *before* the HTTP
call: a bad-arguments error goes back into the transcript as a tool result and
the loop gets another shot, instead of the customer's API receiving garbage.
"""

from typing import Any

import structlog
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

logger = structlog.get_logger(__name__)

# How many individual schema violations we describe before truncating.
MAX_REPORTED_ERRORS = 5


def _describe(error: ValidationError) -> str:
    path = ".".join(str(part) for part in error.absolute_path)
    return f"{path}: {error.message}" if path else error.message


def validate_arguments(schema: dict[str, Any] | None, arguments: dict[str, Any]) -> str | None:
    """Return an error message describing why `arguments` is invalid, else None.

    An empty schema means "the tenant did not declare one" and validates
    anything — this is a customer-supplied field, not a harness guarantee.
    """
    if not schema:
        return None
    try:
        validator = Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(arguments), key=lambda e: list(e.absolute_path))
    except SchemaError as exc:
        # The tenant's own `input_schema` is broken; refuse rather than call
        # the endpoint with unvalidated arguments.
        logger.warning("tools.invalid_input_schema", error=str(exc))
        return f"tool input_schema is not a valid JSON Schema: {exc.message}"

    if not errors:
        return None
    described = [_describe(error) for error in errors[:MAX_REPORTED_ERRORS]]
    hidden = len(errors) - len(described)
    suffix = f" (+{hidden} more)" if hidden > 0 else ""
    return "invalid arguments: " + "; ".join(described) + suffix
