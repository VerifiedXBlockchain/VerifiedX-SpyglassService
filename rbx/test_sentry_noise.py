"""Sentry noise controls: trace sampling and the stale-withdrawal alert dedupe."""

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from project.settings.logging import SENTRY_TRACES_SAMPLE_RATE, sentry_traces_sampler
from rbx.management.commands.health_check import Command

LOCMEM_CACHE = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
}


class TracesSamplerTests(SimpleTestCase):
    def test_masternode_heartbeat_is_never_traced(self):
        by_name = {"transaction_context": {"name": "/api/masternodes/send/"}}
        by_path = {
            "transaction_context": {"name": "generic"},
            "wsgi_environ": {"PATH_INFO": "/api/masternodes/send/"},
        }
        self.assertEqual(sentry_traces_sampler(by_name), 0.0)
        self.assertEqual(sentry_traces_sampler(by_path), 0.0)

    def test_health_check_task_is_never_traced(self):
        by_name = {"transaction_context": {"name": "vfx-explorer.health_check"}}
        by_job = {
            "transaction_context": {"name": "something-else"},
            "celery_job": {"task": "vfx-explorer.health_check"},
        }
        self.assertEqual(sentry_traces_sampler(by_name), 0.0)
        self.assertEqual(sentry_traces_sampler(by_job), 0.0)

    def test_other_transactions_use_the_configured_rate(self):
        context = {"transaction_context": {"name": "/api/transactions/"}}
        self.assertEqual(sentry_traces_sampler(context), SENTRY_TRACES_SAMPLE_RATE)

    def test_parent_decision_is_inherited(self):
        context = {"transaction_context": {"name": "/api/transactions/"}}
        self.assertEqual(sentry_traces_sampler({**context, "parent_sampled": True}), 1.0)
        self.assertEqual(sentry_traces_sampler({**context, "parent_sampled": False}), 0.0)

    def test_empty_context_falls_back_to_the_rate(self):
        self.assertEqual(sentry_traces_sampler({}), SENTRY_TRACES_SAMPLE_RATE)


def _stuck_withdrawal(pk):
    return SimpleNamespace(
        pk=pk,
        token=SimpleNamespace(sc_identifier="abc:1"),
        created_at=datetime(2026, 7, 31, 18, 46),
        amount=Decimal("0.0003"),
        btc_address="bc1qexample",
    )


@override_settings(CACHES=LOCMEM_CACHE)
class StaleWithdrawalAlertTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def _run_with(self, withdrawals):
        queryset = mock.MagicMock()
        queryset.select_related.return_value = withdrawals
        with mock.patch(
            "rbx.management.commands.health_check.VbtcV2WithdrawalRequest"
        ) as model:
            model.objects.filter.return_value = queryset
            model.Status.REQUESTED = "requested"
            with mock.patch(
                "rbx.management.commands.health_check.logging.error"
            ) as error:
                Command().check_stale_withdrawals()
        return error

    def test_each_stuck_withdrawal_is_reported_once_per_day(self):
        withdrawals = [_stuck_withdrawal(11), _stuck_withdrawal(12)]

        first = self._run_with(withdrawals)
        second = self._run_with(withdrawals)

        self.assertEqual(first.call_count, 2)
        self.assertEqual(second.call_count, 0)

    def test_a_new_stuck_withdrawal_is_reported_on_its_first_run(self):
        self._run_with([_stuck_withdrawal(11)])

        later = self._run_with([_stuck_withdrawal(11), _stuck_withdrawal(13)])

        self.assertEqual(later.call_count, 1)
        self.assertIn("withdrawal 13", later.call_args.args[0])
