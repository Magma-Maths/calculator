#!/usr/bin/env bash
set -euo pipefail

pytest -v \
  tests/test_config.py tests/test_executor.py tests/test_executor_io.py \
  tests/test_main.py tests/test_parser.py tests/test_ratelimit.py \
  tests/test_usage_logger.py tests/test_cgroup_bootstrap.py \
  tests/test_containment_probe_cli.py tests/test_containment_controller.py \
  tests/test_integration.py::test_simple_arithmetic \
  tests/test_integration.py::test_variable_and_print \
  tests/test_integration.py::test_multiple_prints \
  tests/test_integration.py::test_error_handling \
  tests/test_integration.py::test_full_response_structure \
  tests/test_integration.py::test_empty_code \
  tests/test_integration.py::test_magma_starts_inside_jail \
  tests/test_integration.py::test_magma_root_symlink_resolved_per_request
