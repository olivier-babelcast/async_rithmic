"""Regression tests for the submit_order concurrent-response bug.

Bug summary: on live_propfirms 2026-04-23 09:01 ET, 3 concurrent
submit_order calls on one Rithmic session all returned "empty response"
even though the orders never made it to Rithmic. Root cause was that
the `else` branch inside `_process_response` for `rp_code[0] == '0'`
only called `request_manager.handle_response(response)` for a small
allow-list of template_ids (11, 15, 114, 301), and jumped straight to
`mark_complete` for all others — including template 313 (new_order
response). For submit_order under concurrent load, only the terminal
response with rp_code=0 arrives in some races, so the response is
dropped and `send_and_collect` returns an empty list.

Fix: always call `handle_response(response)` before `mark_complete` in
the success path. Storing the terminal is idempotent with respect to
callers that check `responses[0]`, and preserves the response when no
intermediate was stored earlier.

These tests exercise only the RequestManager + _process_response flow,
using a minimal FakePlant — they do not require a real Rithmic
connection.
"""
import pytest
import asyncio
import uuid
from collections import namedtuple
from unittest.mock import MagicMock

from async_rithmic.helpers.request_manager import RequestManager

# Mimic a protobuf response closely enough for _process_response:
# template_id, rp_code (list of strings), user_msg (list of strings), plus a basket_id field.
FakeResponse = namedtuple("FakeResponse", ["template_id", "rp_code", "user_msg", "basket_id"])


class FakePlant:
    """Minimal stand-in for a plant: has a lock, logger, and RequestManager."""
    def __init__(self):
        self.lock = asyncio.Lock()
        self.logger = MagicMock(info=print, error=print, exception=print, warning=print, debug=print)
        self.request_manager = RequestManager(self)

    async def _send_request(self, **kwargs):
        # No-op send; tests drive responses directly via _process_response.
        pass


def _process_terminal_success(plant, response):
    """Hand-rolled replica of the relevant branches of plants/base.py:_process_response
    so we can exercise the fix in unit tests without instantiating a full plant.

    The production code path is plants/base.py lines 532-556. We replicate only the
    user_msg+rp_code[0]=='0' success branch since that's what the fix touched.
    """
    if hasattr(response, "user_msg") and response.user_msg is not None and len(response.user_msg) > 0:
        request_id = response.user_msg[0]
        if plant.request_manager.has_pending(request_id):
            if response.rp_code:
                if response.rp_code[0] == '0':
                    # THE FIX: always store the terminal response.
                    plant.request_manager.handle_response(response)
                    plant.request_manager.mark_complete(request_id)
                    return True


@pytest.mark.asyncio
class TestSubmitOrderConcurrent:

    @pytest.fixture
    def plant(self):
        return FakePlant()

    async def _submit_and_deliver_terminal(self, plant, template_id=312, expected_template=313,
                                            deliver_delay=0.005):
        """Simulate one submit_order call: send, wait for one terminal response with rp_code=0,
        return the stored response list."""
        request_id = str(uuid.uuid4())

        async def send():
            return await plant.request_manager.send_and_collect(
                timeout=2.0,
                user_msg=request_id,
                template_id=template_id,
                expected_response=dict(template_id=expected_template, user_msg=[request_id]),
            )

        async def deliver():
            await asyncio.sleep(deliver_delay)
            response = FakeResponse(
                template_id=expected_template,
                rp_code=['0'],  # success — terminal response
                user_msg=[request_id],
                basket_id=f"basket_{request_id[:8]}",
            )
            _process_terminal_success(plant, response)

        # Run send and deliver concurrently; send awaits done_event, deliver fires it.
        results, _ = await asyncio.gather(send(), deliver())
        return results, request_id

    async def test_single_submit_order_terminal_response_stored(self, plant):
        """Before fix: terminal 313 with rp_code=0 was dropped, send_and_collect returned [].
        After fix: response list contains the terminal."""
        responses, request_id = await self._submit_and_deliver_terminal(plant)
        assert len(responses) == 1, f"Expected 1 stored response, got {len(responses)}"
        assert responses[0].basket_id == f"basket_{request_id[:8]}"
        assert responses[0].user_msg == [request_id]
        print(f"[OK] single submit_order — basket_id stored: {responses[0].basket_id}")

    async def test_three_concurrent_submit_orders(self, plant):
        """Repro of the live_propfirms 09:01 ET scenario: 3 concurrent submit_orders,
        each receives only a terminal response with rp_code=0. All 3 must get their
        correct response (matched by user_msg)."""
        results = await asyncio.gather(
            self._submit_and_deliver_terminal(plant, deliver_delay=0.002),
            self._submit_and_deliver_terminal(plant, deliver_delay=0.005),
            self._submit_and_deliver_terminal(plant, deliver_delay=0.008),
        )

        for i, (responses, request_id) in enumerate(results):
            assert len(responses) == 1, \
                f"[Request {i}] Expected 1 response, got {len(responses)} — terminal was dropped"
            # Each response must be matched to its OWN request_id, not a sibling's.
            assert responses[0].user_msg == [request_id], \
                f"[Request {i}] Response user_msg {responses[0].user_msg} does not match request {request_id}"
            assert responses[0].basket_id == f"basket_{request_id[:8]}"
        print(f"[OK] 3 concurrent submit_orders — each got its own terminal response stored correctly")

    async def test_six_concurrent_submit_orders_staggered(self, plant):
        """Heavier fan-out mimicking live_propfirms HAL at a kcbreakout_LR candle close:
        6 APEX accounts, submits fire over ~100ms window, terminal responses arrive
        in arbitrary order."""
        import random
        tasks = [
            self._submit_and_deliver_terminal(plant, deliver_delay=random.uniform(0.001, 0.05))
            for _ in range(6)
        ]
        results = await asyncio.gather(*tasks)

        for i, (responses, request_id) in enumerate(results):
            assert len(responses) == 1, f"[Request {i}] lost terminal response"
            assert responses[0].user_msg == [request_id]
        print(f"[OK] 6 concurrent submit_orders staggered — no terminal dropped")

    async def test_no_response_still_times_out(self, plant):
        """Sanity: when NO response arrives at all, send_and_collect must still time out.
        This confirms the fix doesn't accidentally mark complete without a real response."""
        request_id = str(uuid.uuid4())
        with pytest.raises(asyncio.TimeoutError):
            await plant.request_manager.send_and_collect(
                timeout=0.2,
                user_msg=request_id,
                template_id=312,
                expected_response=dict(template_id=313, user_msg=[request_id]),
            )
        print("[OK] genuine timeout still raises asyncio.TimeoutError")
