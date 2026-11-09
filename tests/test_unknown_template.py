import asyncio
import logging
import struct
from unittest.mock import MagicMock

import async_rithmic.protocol_buffers as pb
from async_rithmic.plants import OrderPlant, TickerPlant
from async_rithmic.plants import base as pbase


def _frame(message) -> bytes:
    raw = message.SerializeToString()
    return struct.pack(">I", len(raw)) + raw


def _unknown_message(template_id=358):
    # Any known message re-labelled with a template id the library has no class for.
    return pb.account_rms_updates_pb2.AccountRmsUpdates(
        template_id=template_id, fcm_id="AMP", ib_id="AMP", account_id="216916")


def test_unknown_template_is_skipped_and_its_fields_named(caplog):
    plant = OrderPlant(MagicMock())
    with caplog.at_level(logging.WARNING):
        assert plant._convert_bytes_to_response(_frame(_unknown_message())) is None
    line = caplog.records[-1].getMessage()
    assert "Unknown template ID 358 skipped (#1)" in line
    assert "('account_id', '216916')" in line
    assert "('template_id', 358)" in line


def test_unknown_template_logs_first_messages_then_a_count(caplog):
    plant = OrderPlant(MagicMock())
    with caplog.at_level(logging.WARNING):
        for _ in range(pbase.UNKNOWN_TEMPLATE_LOG_EVERY):
            plant._convert_bytes_to_response(_frame(_unknown_message()))
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == pbase.UNKNOWN_TEMPLATE_LOG_FIRST + 1
    assert lines[-1] == (f"Unknown template ID 358: {pbase.UNKNOWN_TEMPLATE_LOG_EVERY} "
                         f"messages skipped so far")


def test_known_template_still_decodes():
    plant = TickerPlant(MagicMock())
    msg = pb.response_heartbeat_pb2.ResponseHeartbeat(template_id=19)
    response = plant._convert_bytes_to_response(_frame(msg))
    assert isinstance(response, pb.response_heartbeat_pb2.ResponseHeartbeat)


def test_process_loop_skips_unknown_and_keeps_going():
    plant = TickerPlant(MagicMock())
    processed = []

    async def record(response):
        processed.append(response.template_id)

    plant._process_response = record

    async def run():
        plant._inbound_queue = asyncio.Queue()
        await plant._inbound_queue.put(_frame(_unknown_message()))
        await plant._inbound_queue.put(_frame(pb.response_heartbeat_pb2.ResponseHeartbeat(template_id=19)))
        task = asyncio.create_task(plant._process_loop())
        for _ in range(50):
            if processed:
                break
            await asyncio.sleep(0.01)
        task.cancel()

    asyncio.run(run())
    assert processed == [19]


def test_decode_wire_fields_on_garbage_does_not_raise():
    assert pbase.decode_wire_fields(b"\x0a\x05ab")[-1][0] == "truncated"
