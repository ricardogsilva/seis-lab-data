from typing import Any

_ERROR_CONTENT: dict[str, Any] = {
    "application/json": {
        "schema": {
            "type": "object",
            "properties": {"detail": {"type": "string"}},
        }
    }
}

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"description": "Unauthorized", "content": _ERROR_CONTENT},
    422: {"description": "Unprocessable Entity"},
    500: {"description": "Internal Server Error", "content": _ERROR_CONTENT},
}
