def incompatible_result(number: int) -> str:
    return number


def modern_syntax(number: int | None) -> list[int] | None:
    return [number] if number is not None else None
