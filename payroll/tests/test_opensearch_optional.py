"""The OpenSearch integration is optional and must degrade quietly.

`opensearch_reports` can be pip-installed while absent from INSTALLED_APPS —
a common state when the package arrives as a transitive dependency. Importing
its models in that state raises RuntimeError, not ImportError:

    RuntimeError: Model class opensearch_reports.models.OpenSearchDashboard
    doesn't declare an explicit app_label and isn't in an application in
    INSTALLED_APPS.

A guard of `except ImportError` does not catch it, so benefit generation raises
and the payroll is persisted as FAILED with zero benefits.
"""
from unittest.mock import patch

from django.apps import apps
from django.test import TestCase

from payroll.services import get_opensearch_dashboard_model


class OpenSearchOptionalImportTests(TestCase):
    def test_returns_none_when_app_not_registered(self):
        """No exception, whatever state the package is in."""
        with patch.dict(apps.app_configs, clear=False) as configs:
            configs.pop('opensearch_reports', None)
            self.assertIsNone(
                get_opensearch_dashboard_model(),
                "an unregistered app must yield None rather than raising")

    def test_returns_model_when_app_registered(self):
        if 'opensearch_reports' not in apps.app_configs:
            self.skipTest("opensearch_reports is not installed in this assembly")
        self.assertIsNotNone(get_opensearch_dashboard_model())

    def test_matches_documents_module_convention(self):
        """documents.py already gates on the app registry; services must agree."""
        registered = 'opensearch_reports' in apps.app_configs
        self.assertEqual(
            get_opensearch_dashboard_model() is not None, registered,
            "availability must follow app registration, not package presence")
