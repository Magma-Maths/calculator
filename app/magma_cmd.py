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

    The launcher (magma_root/magma) is a shell script and the jail mounts no
    shell and no /usr/bin, so the binary is exec'd directly and gets these
    from nsjail --env instead. The launcher's constant exports are in
    nsjail.cfg.
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
