import pytest
from bitcoin_client.bitcoin_cmd import BitcoinCommand
from bitcoin_client.exception import IncorrectLengthError

from ragger.backend import RaisePolicy


def test_random(backend, firmware):
    cmd = BitcoinCommand(transport=backend, debug=False)
    r: bytes = cmd.get_random(n=5)
    assert len(r) == 5

    r = cmd.get_random(n=32)
    assert len(r) == 32

    # max length is 248!
    with pytest.raises(IncorrectLengthError):
        backend.raise_policy = RaisePolicy.RAISE_NOTHING
        cmd.get_random(n=249)
