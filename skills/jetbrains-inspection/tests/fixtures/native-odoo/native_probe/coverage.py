"""Positive controls for the Python checks absent from Community Qodana."""
INSECURE = "http://example.com/native-probe"
SECURE = "https://example.com/native-probe"


def broken_reference():
    return jb_native_missing_name
