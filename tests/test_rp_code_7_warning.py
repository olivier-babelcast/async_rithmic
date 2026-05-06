"""Regression tests for the scoped rp_code=7 WARNING.

Background: when Rithmic refuses an order it returns rp_code=7 ("no data")
on the corresponding response template (313 submit ack, 315 modify ack,
317 cancel ack, 331 bracket ack). Pre-fix, the library swallowed these at
DEBUG and returned an empty list to the caller. From the wrapper's point of
view the order had simply "timed out" with no visibility into why.

Fix: log at WARNING when rp_code=7 fires on an order-path response, leave
DEBUG behavior intact for non-order endpoints (instrument lookup,
historical data) which legitimately return rp_code=7 for "no data".

These tests exercise the `_process_response` branch directly via a hand-
rolled replica (same pattern as `test_submit_order_concurrent.py`).
"""
import asyncio
from collections import namedtuple
from unittest.mock import MagicMock

import pytest

from async_rithmic.helpers.request_manager import RequestManager


FakeResponse = namedtuple("FakeResponse", ["template_id", "rp_code", "user_msg"])


class FakePlant:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.logger = MagicMock()
        self.request_manager = RequestManager(self)


def _process_rp_code_7(plant, response):
    """Replica of the `rp_code[0] == '7'` branch in plants/base.py:_process_response.

    Production code path: lines 538-558. Mirrors the new scoped-WARNING logic.
    """
    request_id = response.user_msg[0]
    if not plant.request_manager.has_pending(request_id):
        return False
    if not response.rp_code or response.rp_code[0] != '7':
        return False

    _order_path = {313, 315, 317, 331}
    if response.template_id in _order_path:
        request = plant.request_manager.requests.get(request_id)
        plant.logger.warning(
            f"Rithmic refused order (rp_code=7) "
            f"template_id={response.template_id} "
            f"request_id={request_id} "
            f"response={dict(template_id=response.template_id, rp_code=list(response.rp_code))} "
            f"request={request}"
        )
    else:
        plant.logger.debug(f"Rithmic returned no data for request_id={request_id}")
    plant.request_manager.mark_complete(request_id)
    return True


@pytest.fixture
def plant():
    return FakePlant()


def _start(plant, request_id, template_id):
    plant.request_manager.start(
        request_id,
        request={"template_id": template_id},
        expected_response={"template_id": template_id + 1},
    )


def _no_data_debug_calls(logger):
    """Filter out RequestManager's own DEBUG bookkeeping ("Completed request ...")
    so we can assert specifically on the "Rithmic returned no data" line our branch emits."""
    return [c for c in logger.debug.call_args_list if "no data" in (c.args[0] if c.args else "")]


@pytest.mark.parametrize("template_id", [313, 315, 317, 331])
def test_warning_on_order_path_rp_code_7(plant, template_id):
    """rp_code=7 on submit/modify/cancel/bracket ack templates → WARNING, no DEBUG fallback."""
    request_id = "req-order-1"
    _start(plant, request_id, template_id - 1)

    response = FakeResponse(template_id=template_id, rp_code=["7"], user_msg=[request_id])
    handled = _process_rp_code_7(plant, response)

    assert handled is True
    plant.logger.warning.assert_called_once()
    msg = plant.logger.warning.call_args[0][0]
    assert "rp_code=7" in msg
    assert f"template_id={template_id}" in msg
    assert _no_data_debug_calls(plant.logger) == []


@pytest.mark.parametrize("template_id", [203, 205, 251, 261, 305])
def test_debug_on_non_order_path_rp_code_7(plant, template_id):
    """rp_code=7 on data-fetch / instrument-lookup endpoints stays at DEBUG."""
    request_id = "req-data-1"
    _start(plant, request_id, template_id - 1)

    response = FakeResponse(template_id=template_id, rp_code=["7"], user_msg=[request_id])
    handled = _process_rp_code_7(plant, response)

    assert handled is True
    no_data_calls = _no_data_debug_calls(plant.logger)
    assert len(no_data_calls) == 1
    plant.logger.warning.assert_not_called()


def test_no_warning_for_unknown_request(plant):
    """Stale rp_code=7 for a request we don't track anymore → no log lines fire."""
    response = FakeResponse(template_id=313, rp_code=["7"], user_msg=["unknown"])
    handled = _process_rp_code_7(plant, response)

    assert handled is False
    plant.logger.warning.assert_not_called()
    plant.logger.debug.assert_not_called()


def test_no_warning_for_rp_code_zero(plant):
    """rp_code=0 success → not our branch, no log fires."""
    request_id = "req-success"
    _start(plant, request_id, 312)

    response = FakeResponse(template_id=313, rp_code=["0"], user_msg=[request_id])
    handled = _process_rp_code_7(plant, response)

    assert handled is False  # branch only triggers on rp_code=7
    plant.logger.warning.assert_not_called()
    plant.logger.debug.assert_not_called()
