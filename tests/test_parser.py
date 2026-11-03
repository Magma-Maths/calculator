from app.parser import parse_magma_output


SAMPLE_BANNER = "Magma V2.29-4     Fri Jan 31 2026 12:00:00 on linux   [Seed = 1234567890]\n"
SAMPLE_QUIT = "quit.\n"
SAMPLE_BODY = "2\n"
SAMPLE_FOOTER = "Total time: 0.050 seconds, Total memory usage: 12.34MB\n"


def test_parse_simple_output():
    stdout = SAMPLE_BANNER + SAMPLE_QUIT + SAMPLE_BODY + SAMPLE_FOOTER
    result = parse_magma_output(stdout, max_output_bytes=20480)
    assert result.version == "2.29-4"
    assert result.seed == 1234567890
    assert result.stdout == "2\n"
    assert result.time_sec == 0.05
    assert result.memory == "12.34MB"
    assert result.truncated is False
    assert result.warnings == []


def test_parse_backslash_wrapped_footer():
    # Captured from Magma V2.29-10 with SetColumns(14); print 42;.
    rest = "42\n\nTotal time: \n0.000 seconds\\\n, Total memory\nusage: 32.09M\\\nB\n"
    result = parse_magma_output(SAMPLE_BANNER + SAMPLE_QUIT + rest, max_output_bytes=20480)
    assert result.stdout == "42\n"
    assert result.time_sec == 0.0
    assert result.memory == "32.09MB"


def test_parse_backslash_wrapped_footer_after_long_body():
    body = "x" * 600 + "\n"
    rest = "42\n\nTotal time: \n0.000 seconds\\\n, Total memory\nusage: 32.09M\\\nB\n"
    result = parse_magma_output(SAMPLE_BANNER + SAMPLE_QUIT + body + rest, max_output_bytes=20480)
    assert result.stdout == body + "42\n"
    assert result.time_sec == 0.0
    assert result.memory == "32.09MB"


def test_parse_footer_after_user_backslash():
    rest = "answer\\\nTotal time: 0.000 seconds, Total memory usage: 32.09MB\n"
    result = parse_magma_output(SAMPLE_BANNER + SAMPLE_QUIT + rest, max_output_bytes=20480)
    assert result.stdout == "answer\\\n"
    assert result.time_sec == 0.0
    assert result.memory == "32.09MB"


def test_parse_extracts_version_variants():
    stdout = "Magma V2.28     Fri Jan 31 2026 [Seed = 999]\nquit.\nok\nTotal time: 1.000 seconds, Total memory usage: 5.00MB\n"
    result = parse_magma_output(stdout, max_output_bytes=20480)
    assert result.version == "2.28"
    assert result.seed == 999


def test_parse_multiline_body():
    body = "line1\nline2\nline3\n"
    stdout = SAMPLE_BANNER + SAMPLE_QUIT + body + SAMPLE_FOOTER
    result = parse_magma_output(stdout, max_output_bytes=20480)
    assert result.stdout == body


def test_parse_strips_machine_type():
    body = "Machine type: X86_64-linux\nresult\n"
    stdout = SAMPLE_BANNER + SAMPLE_QUIT + body + SAMPLE_FOOTER
    result = parse_magma_output(stdout, max_output_bytes=20480)
    assert "Machine type" not in result.stdout
    assert "result\n" in result.stdout


def test_parse_truncates_long_output():
    body = "x" * 100 + "\n"
    stdout = SAMPLE_BANNER + SAMPLE_QUIT + body + SAMPLE_FOOTER
    result = parse_magma_output(stdout, max_output_bytes=50)
    assert len(result.stdout) == 50
    assert result.truncated is True
    assert "The output is too long and has been truncated." in result.warnings


def test_parse_detects_memory_limit():
    body = "User memory limit exceeded\n"
    stdout = SAMPLE_BANNER + SAMPLE_QUIT + body + SAMPLE_FOOTER
    result = parse_magma_output(stdout, max_output_bytes=20480)
    assert any("memory limit" in w for w in result.warnings)


def test_parse_detects_runtime_error():
    body = "Runtime error in 'foo': something\n"
    stdout = SAMPLE_BANNER + SAMPLE_QUIT + body + SAMPLE_FOOTER
    result = parse_magma_output(stdout, max_output_bytes=20480)
    assert any("error occurred" in w.lower() for w in result.warnings)


def test_parse_detects_user_error():
    body = "User error: bad input\n"
    stdout = SAMPLE_BANNER + SAMPLE_QUIT + body + SAMPLE_FOOTER
    result = parse_magma_output(stdout, max_output_bytes=20480)
    assert any("error occurred" in w.lower() for w in result.warnings)


def test_parse_detects_internal_error():
    body = "Something (internal error)\n"
    stdout = SAMPLE_BANNER + SAMPLE_QUIT + body + SAMPLE_FOOTER
    result = parse_magma_output(stdout, max_output_bytes=20480)
    assert any("error occurred" in w.lower() for w in result.warnings)


def test_parse_detects_illegal_syscall():
    body = "Illegal system call\n"
    stdout = SAMPLE_BANNER + SAMPLE_QUIT + body + SAMPLE_FOOTER
    result = parse_magma_output(stdout, max_output_bytes=20480)
    assert any("error occurred" in w.lower() for w in result.warnings)


def test_parse_empty_body():
    stdout = SAMPLE_BANNER + SAMPLE_QUIT + SAMPLE_FOOTER
    result = parse_magma_output(stdout, max_output_bytes=20480)
    assert result.stdout == ""


def test_parse_no_footer():
    stdout = SAMPLE_BANNER + SAMPLE_QUIT + "partial output\n"
    result = parse_magma_output(stdout, max_output_bytes=20480)
    assert result.stdout == "partial output\n"
    assert result.time_sec is None
    assert result.memory is None


def test_parse_no_banner():
    stdout = ""
    result = parse_magma_output(stdout, max_output_bytes=20480)
    assert result.version is None
    assert result.seed is None
    assert result.stdout == ""


from app.parser import parse_stderr_warnings


def test_stderr_alarm_clock():
    warnings = parse_stderr_warnings("Alarm clock\n")
    assert len(warnings) == 1
    assert "time limit" in warnings[0]


def test_stderr_cputime_limit():
    warnings = parse_stderr_warnings("Cputime limit exceeded\n")
    assert len(warnings) == 1
    assert "time limit" in warnings[0]


def test_stderr_killed():
    warnings = parse_stderr_warnings("Killed\n")
    assert len(warnings) == 1
    assert "time limit" in warnings[0]


def test_stderr_fatal_error():
    warnings = parse_stderr_warnings("Magma: Fatal Error\n")
    assert len(warnings) == 1
    assert "fatal error" in warnings[0].lower()


def test_stderr_empty():
    warnings = parse_stderr_warnings("")
    assert warnings == []


def test_stderr_none():
    warnings = parse_stderr_warnings(None)
    assert warnings == []


def test_stderr_unknown():
    """Unknown stderr content is ignored (not passed to user)."""
    warnings = parse_stderr_warnings("some random debug output\n")
    assert warnings == []
