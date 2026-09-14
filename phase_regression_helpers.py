"""Isolate accepted Phase 2A-C unit scenarios from Phase 2D completeness routing.

These old fixtures intentionally omit building sizing. Production finalization is tested
without this mock in test_system_sizing, including whole-report legacy-fixture checks.
No production flag or alternate policy is introduced.
"""
from unittest.mock import patch
import main


def finalize_prior_module_analysis(*args, **kwargs):
    with patch.object(main, "sizing_required", return_value=False):
        return main.finalize_customer_analysis(*args, **kwargs)
