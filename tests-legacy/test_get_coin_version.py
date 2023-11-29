from bitcoin_client.bitcoin_cmd import BitcoinCommand

def test_get_coin_version(backend, firmware):
    cmd = BitcoinCommand(transport=backend, debug=False)
    (p2pkh_prefix, p2sh_prefix, coin_family, coin_name, coin_ticker) = cmd.get_coin_version()

    # Bitcoin app: (0x00, 0x05, 0x01, "Bitcoin", "BTC")
    assert (p2pkh_prefix,
            p2sh_prefix,
            coin_family,
            coin_name,
            coin_ticker) == (0x6F, 0xC4, 0x01, "Raven", "TRVN")
