"""
Sanity checks for app.pii.mask_phone -- the one shared helper every logger.* call uses
instead of printing a patient's full phone number. No DB/Redis dependency, no stubs needed.
Run directly: python3 tests/test_pii.py
"""

import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.pii import mask_phone  # noqa: E402

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"PASS: {message}")


def test_keeps_only_the_last_four_digits():
    check(mask_phone("919876543210") == "********3210", "masks everything but the last 4 digits")


def test_none_and_empty_are_safe():
    check(mask_phone(None) == "****", "None doesn't crash, returns a generic mask")
    check(mask_phone("") == "****", "empty string doesn't crash")


def test_short_strings_are_fully_masked_not_leaked():
    check(mask_phone("123") == "***", "a string shorter than the reveal window is fully masked, not returned as-is")
    check(mask_phone("1234") == "****", "exactly 4 chars is fully masked too -- keeping it would defeat the point")


def test_never_returns_the_original_value_for_a_real_number():
    phone = "917739668546"
    check(phone not in mask_phone(phone), "the real number never appears verbatim in the masked output")


if __name__ == "__main__":
    test_keeps_only_the_last_four_digits()
    test_none_and_empty_are_safe()
    test_short_strings_are_fully_masked_not_leaked()
    test_never_returns_the_original_value_for_a_real_number()

    print("\n" + "=" * 50)
    if failures:
        print(f"PII TESTS FAILED with {len(failures)} errors")
        sys.exit(1)
    else:
        print("ALL PII TESTS PASSED")
