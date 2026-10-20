"""Generic type checker control, also used without the Odoo framework."""


def wrong_type(value: int) -> str:
    return value


def valid_modern_annotation(value: int | None = None) -> dict[str, int] | None:
    return {"value": value or 0}
