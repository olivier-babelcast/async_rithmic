import logging
import struct
from unittest.mock import MagicMock

import pytest

import async_rithmic.protocol_buffers as pb
from async_rithmic.exceptions import RithmicErrorResponse
from async_rithmic.plants import OrderPlant
from async_rithmic.plants import base as pbase

PASSWORD = "s3cret-pass"
LOGIN = dict(template_id=10, template_version="3.9", user="someone@amp.com", password=PASSWORD,
             system_name="Rithmic 01", app_name="app", app_version="1.0", infra_type=2)


def _frame(message) -> bytes:
    raw = message.SerializeToString()
    return struct.pack(">I", len(raw)) + raw


class _Socket:
    """Answers every request with one canned response."""

    def __init__(self, response):
        self.sent, self.response = [], response

    async def send(self, buffer):
        self.sent.append(buffer)

    async def recv(self):
        return _frame(self.response)


def _refusing_plant():
    plant = OrderPlant(MagicMock())
    plant.ws = _Socket(pb.response_login_pb2.ResponseLogin(template_id=11, rp_code=["13", "permission denied"]))
    return plant


def test_redact_masks_the_password_and_keeps_the_rest():
    assert pbase.redact(LOGIN) == {**LOGIN, "password": "***"}


async def test_a_refused_login_raises_without_the_password():
    plant = _refusing_plant()
    with pytest.raises(RithmicErrorResponse) as err:
        await plant._send_and_recv_immediate(**LOGIN)
    message = str(err.value)
    assert "permission denied" in message and "someone@amp.com" in message
    assert PASSWORD not in message and "'password': '***'" in message
    assert PASSWORD.encode() in plant.ws.sent[0]          # the request itself still carries it


async def test_debug_logging_never_prints_the_password(caplog):
    plant = _refusing_plant()
    with caplog.at_level(logging.DEBUG, logger=plant.logger.name):
        with pytest.raises(RithmicErrorResponse):
            await plant._send_and_recv_immediate(**LOGIN)
    lines = [r.getMessage() for r in caplog.records]
    assert any(line.startswith("Sending message") for line in lines)
    assert not any(PASSWORD in line for line in lines)
