COMMAND_PREFIX = "guess"


def command_usage(*parts: str) -> str:
    suffix = " ".join(part for part in parts if part)
    return f"/{COMMAND_PREFIX}" if not suffix else f"/{COMMAND_PREFIX} {suffix}"
