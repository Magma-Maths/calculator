import re
from dataclasses import dataclass, field


@dataclass
class ParseResult:
    version: str | None = None
    seed: int | None = None
    stdout: str = ""
    time_sec: float | None = None
    memory: str | None = None
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)


_RE_VERSION = re.compile(r"Magma V(\d+\.\d+(-[A-Z]*\d+)?)")
_RE_SEED = re.compile(r"\[Seed = (\d+)\]")
_RE_FOOTER_START = re.compile(r"Total time:\s+\d+\.\d+ seconds, Total memory usage: ")
_RE_TIME = re.compile(r"Total time:\s+(\d+\.\d+)")
_RE_MEMORY = re.compile(r"Total memory usage: (\d+\.\d+[A-Z]+)")
_RE_MACHINE_TYPE = re.compile(r"Machine type: .*\n")

_ERROR_PATTERNS = [
    "User error: ",
    "Runtime error in ",
    "(internal error)",
    "Illegal system call",
]


def parse_magma_output(stdout: str, max_output_bytes: int) -> ParseResult:
    result = ParseResult()

    if not stdout:
        return result

    quit_idx = stdout.find("quit.\n")
    if quit_idx == -1:
        _extract_banner(stdout, result)
        return result

    banner = stdout[:quit_idx]
    rest = stdout[quit_idx + len("quit.\n"):]

    _extract_banner(banner, result)

    footer_match = _RE_FOOTER_START.search(rest)
    if footer_match:
        body = rest[:footer_match.start()]
        footer = rest[footer_match.start():]
        _extract_footer(footer, result)
    else:
        body = rest

    body = body.rstrip("\n")
    if body:
        body += "\n"
    else:
        body = ""

    body = _RE_MACHINE_TYPE.sub("", body)

    if "User memory limit" in body:
        result.warnings.append(
            "The computation exceeded the memory limit and so was terminated prematurely."
        )

    for pattern in _ERROR_PATTERNS:
        if pattern in body:
            result.warnings.append("An error occurred. See the output for details.")
            break

    if len(body) > max_output_bytes:
        body = body[:max_output_bytes]
        result.truncated = True
        result.warnings.append("The output is too long and has been truncated.")

    result.stdout = body
    return result


def _extract_banner(text: str, result: ParseResult) -> None:
    m = _RE_VERSION.search(text)
    if m:
        result.version = m.group(1)
    m = _RE_SEED.search(text)
    if m:
        result.seed = int(m.group(1))


def _extract_footer(text: str, result: ParseResult) -> None:
    m = _RE_TIME.search(text)
    if m:
        result.time_sec = float(m.group(1))
    m = _RE_MEMORY.search(text)
    if m:
        result.memory = m.group(1)
