# The launcher script's constant exports; see magma_environment().
MAGMA_CONSTANT_ENV: dict[str, str] = {
    "MAGMA_LIBRARIES": "c9lattices:examples:galpols:intro:isolgps:matgps:pergps:simgps:solgps",
    "MKL_SERIAL": "YES",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
}


def wrap_magma_code(code: str, timeout: int) -> str:
    alarm_timeout = timeout - 1
    return (
        f"Alarm({alarm_timeout});\n"
        f"SetIgnorePrompt(true);\n"
        f"{code}\n"
        f";\n"
        f"quit;\n"
    )


def magma_environment(magma_root: str) -> list[str]:
    """Root-dependent variables the magma launcher script exports for magma.exe.

    The guest execs magma.exe directly rather than through the launcher
    (magma_root/magma), a shell script, so it needs these set for it. The
    launcher's constant exports are MAGMA_CONSTANT_ENV.
    """
    root = magma_root.rstrip("/")
    return [
        f"MAGMA_CMD={root}/magma",
        f"MAGMAPASSFILE={root}/magmapassfile",
        f"MAGMA_SYSTEM_SPEC={root}/package/spec",
        f"MAGMA_SYSTEM_PACKAGE_ROOT={root}/package",
        f"MAGMA_LIBRARY_ROOT={root}/libs",
        f"MAGMA_HELP_DIR={root}/InternalHelp",
        f"MAGMA_HTML_DIR={root}/doc/html",
    ]
