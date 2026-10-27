"""Positive controls for the Python checks absent from Community Qodana."""
INSECURE = "http://example.com/native-probe"
SECURE = "https://example.com/native-probe"


def broken_reference():
    return jb_native_missing_name


def valid_builtins(values: list[int]) -> tuple[int, int]:
    print(values)
    return len(values), max(values, default=0)
