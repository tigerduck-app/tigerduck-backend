"""The student IDs an operator types."""


def student_id_key(typed: str) -> str:
    """`typed` as the backend keys an account by it: trimmed and upper-cased,
    as `POST /v3/auth/login` stores it."""
    return typed.strip().upper()
