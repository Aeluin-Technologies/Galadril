"""Runs the shared native SSO fixture without duplicating Bazel compilation."""

import unittest

from infrastructure.auth.tests import test_sso

if __name__ == "__main__":
    unittest.main(module=test_sso)
