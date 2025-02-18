from typing import Tuple, List

from ledgercomm import Transport

from bitcoin_client.hwi.serialization import (CTransaction, CTxIn, CTxOut, COutPoint,
                                              is_witness, is_p2wpkh, is_p2pkh, is_p2sh, hash160)
from bitcoin_client.hwi.bech32 import decode as bech32_decode
from bitcoin_client.hwi.base58 import decode as base58_decode
from bitcoin_client.utils import deser_trusted_input
from bitcoin_client.bitcoin_utils import bip143_digest, compress_pub_key
from bitcoin_client.bitcoin_cmd_builder import AddrType
from bitcoin_client.bitcoin_base_cmd import BitcoinBaseCommand


class BitcoinCommand(BitcoinBaseCommand):
    """Bitcoin Command.

    Inherit from BitcoinBaseCommand and provide a high level
    interface to sign Bitcoin transaction.

    Parameters
    ----------
    transport : Transport
        Transport interface to the device.
    debug : bool
        Whether you want to see logging or not.

    """

    def __init__(self, transport: Transport, debug: bool = False) -> None:
        """Init constructor."""
        super().__init__(transport, debug)

    def sign_new_tx(self,
                    address: str,
                    amount: int,
                    fees: int,
                    change_path: str,
                    sign_paths: List[str],
                    raw_utxos: List[Tuple[bytes, int]],
                    lock_time: int = 0) -> List[Tuple[bytes, bytes, Tuple[int, bytes]]]:
        """Sign a new transaction with parameters..

        Parameters
        ----------
        address : str
            Bitcoin address.
        amount : int
            Amount to send to address in satoshis.
        fees : int
            Fees of the new transaction.
        change_path : str
            BIP32 path for the change.
        sign_paths : List[str]
            BIP32 paths to sign inputs.
        raw_utxos : List[Tuple[bytes, int]]
            Pairs of raw hex transaction and output index to use as UTXOs.
        lock_time : int
            Block height or timestamp when transaction is final.

        Returns
        -------
        List[Tuple[bytes, bytes, Tuple[int, bytes]]]
            Tuples (tx_hash_digest, sign_pub_key, (v, der_sig))

        """
        utxos: List[Tuple[CTransaction, int, int]] = []
        amount_available: int = 0
        for raw_tx, output_index in raw_utxos:
            utxo = CTransaction.from_bytes(raw_tx)
            value = utxo.vout[output_index].nValue
            utxos.append((utxo, output_index, value))
            amount_available += value

        sign_pub_keys: List[bytes] = []
        for sign_path in sign_paths:
            sign_pub_key, _, _ = self.get_public_key(
                addr_type=AddrType.Legacy,  # AddrType.BECH32,
                bip32_path=sign_path,
                display=False
            )
            sign_pub_keys.append(compress_pub_key(sign_pub_key))

        inputs: List[Tuple[CTransaction, bytes]] = [
            (utxo, self.get_trusted_input(utxo=utxo, output_index=output_index))
            for utxo, output_index, _ in utxos
        ]

        # new transaction
        tx: CTransaction = CTransaction()
        tx.nVersion = 2
        tx.nLockTime = lock_time
        # prepare vin
        for i, (utxo, trusted_input) in enumerate(inputs):
            if utxo.sha256 is None:
                utxo.calc_sha256(with_witness=False)

            _, _, _, prev_txid, output_index, _, _ = deser_trusted_input(trusted_input)
            assert prev_txid != utxo.sha256

            script_pub_key: bytes = utxo.vout[output_index].scriptPubKey
            # P2WPKH
            if is_p2wpkh(script_pub_key):
                _, _, wit_prog = is_witness(script_pub_key)
                script_pub_key = (b"\x76" +  # OP_DUP
                                  b"\xa9" +  # OP_HASH160
                                  b"\x14" +  # bytes to push (20)
                                  wit_prog +  # hash160(pubkey)
                                  b"\x88" +  # OP_EQUALVERIFY
                                  b"\xac")  # OP_CHECKSIG
            # P2SH-P2WPKH or P2PKH
            if (is_p2sh(script_pub_key) and not utxo.wit.is_null()) or is_p2pkh(script_pub_key):
                script_pub_key = (b"\x76" +  # OP_DUP
                                  b"\xa9" +  # OP_HASH160
                                  b"\x14" +  # bytes to push (20)
                                  hash160(sign_pub_keys[i]) +  # hash160(pubkey)
                                  b"\x88" +  # OP_EQUALVERIFY
                                  b"\xac")  # OP_CHECKSIG
            tx.vin.append(CTxIn(outpoint=COutPoint(h=utxo.sha256, n=output_index),
                                scriptSig=script_pub_key,
                                nSequence=0xfffffffd))

        if amount_available - fees > amount:
            change_pub_key, _, _ = self.get_public_key(
                addr_type=AddrType.Legacy,  # AddrType.BECH32,
                bip32_path=change_path,
                display=False
            )
            change_pubkey_hash = hash160(compress_pub_key(change_pub_key))
            change_script_pubkey: bytes
            # Bech32 pubkey hash or script hash (mainnet and testnet)
            if address.startswith("bc1") or address.startswith("tb1"):
                change_script_pubkey = bytes([0, len(change_pubkey_hash)]) + change_pubkey_hash
            # P2SH-P2WPKH (mainnet and testnet)
            elif address.startswith("3") or address.startswith("2"):
                change_script_pubkey = (b"\xa9" +  # OP_HASH160
                                        b"\x14" +  # bytes to push (20)
                                        # hash160(redeem_script)
                                        hash160(bytes([0, len(change_pubkey_hash)]) + change_pubkey_hash) +
                                        b"\x87")  # OP_EQUAL
            # P2PKH address (mainnet and testnet)
            elif address.startswith("1") or (address.startswith("m") or address.startswith("n")):
                change_script_pubkey = (b"\x76" +  # OP_DUP
                                        b"\xa9" +  # OP_HASH160
                                        b"\x14" +  # bytes to push (20)
                                        change_pubkey_hash +  # hash160(pubkey)
                                        b"\x88" +  # OP_EQUALVERIFY
                                        b"\xac")  # OP_CHECKSIG
            else:
                raise Exception(f"Unsupported address: '{address}'")
            tx.vout.append(
                CTxOut(nValue=amount_available - fees - amount,
                       scriptPubKey=change_script_pubkey)
            )

        script_pub_key: bytes
        # Bech32 pubkey hash or script hash (mainnet and testnet)
        if address.startswith("bc1") or address.startswith("tb1"):
            witness_version, witness_program = bech32_decode(address[0:2], address)
            script_pub_key = bytes(
                [witness_version + 0x50 if witness_version else 0,
                 len(witness_program)] +
                witness_program
            )
        # P2SH address (mainnet and testnet)
        elif address.startswith("3") or address.startswith("2"):
            script_pub_key = (b"\xa9" +  # OP_HASH160
                              b"\x14" +  # bytes to push (20)
                              base58_decode(address)[1:-4] +  # hash160(redeem_script)
                              b"\x87")  # OP_EQUAL
        # P2PKH address (mainnet and testnet)
        elif address.startswith("1") or (address.startswith("m") or address.startswith("n")):
            script_pub_key = (b"\x76" +  # OP_DUP
                              b"\xa9" +  # OP_HASH160
                              b"\x14" +  # bytes to push (20)
                              base58_decode(address)[1:-4] +  # hash160(pubkey)
                              b"\x88" +  # OP_EQUALVERIFY
                              b"\xac")  # OP_CHECKSIG
        else:
            raise Exception(f"Unsupported address: '{address}'")

        tx.vout.append(CTxOut(nValue=amount,
                              scriptPubKey=script_pub_key))

        ''' TEST DISPLAY

        # Transfer with max name length & amount len
        tx.vout.append(CTxOut(nValue=0,
                              scriptPubKey=script_pub_key +
                              bytes([
                                  0xc0, 44,
                                  0x72, 0x76, 0x6E,
                                  0x74,
                                  0x1F,
                                  0x72, 0x72, 0x72, 0x72, 0x72, 0x72, 0x72, 0x72, 0x72, 0x72, 0x72, 0x72, 0x72, 0x72, 0x72, 0x72,
                                  0x72, 0x72, 0x72, 0x72, 0x72, 0x72, 0x72, 0x72, 0x72, 0x72, 0x72, 0x72, 0x72, 0x72, 0x72,
                                  0xff, 0xff, 0x51, 0xac, 0xdf, 0xb2, 0x24, 0x1d,
                                  0x75
                              ])))

        # Ownership asset
        tx.vout.append(CTxOut(nValue=0,
                              scriptPubKey=script_pub_key +
                              bytes([
                                  0xc0, 0x09,
                                  0x72, 0x76, 0x6E,
                                  0x6F,
                                  0x04,
                                  0x72, 0x72, 0x72, 0x21,
                                  0x75
                              ])))

        # Asset creation w/ ipfs
        tx.vout.append(CTxOut(nValue=0,
                              scriptPubKey=script_pub_key +
                              bytes([
                                  0xc0, 47+34-31+10,
                                  0x72, 0x76, 0x6E,
                                  0x71,
                                  10,
                                  0x54, 0x45, 0x53, 0x54, 0x5F, 0x41, 0x53, 0x53, 0x45, 0x54,
                                  0x00, 0xE1, 0xF5, 0x05, 0x00, 0x00, 0x00, 0x00,
                                  0, 1, 1,
                                  0x12, 0x20, 0x84, 0x43, 0xbc, 0xbb, 0x6a, 0x01, 0x18, 0xae, 0xbf, 0xcf, 0xe9, 0x1c, 0x12, 0x5d, 0x6e,
                                  0x58, 0xa8, 0x76, 0x93, 0xb7, 0x3d, 0x08, 0xf7, 0x7d, 0x77, 0xf6, 0xe7, 0x8f, 0xa2, 0x29, 0x56, 0x3c,
                                  0x75
                              ])))

        # Asset creation w/ no ipfs
        tx.vout.append(CTxOut(nValue=0,
                              scriptPubKey=script_pub_key +
                              bytes([
                                  0xc0, 47-31+10,
                                  0x72, 0x76, 0x6E,
                                  0x71,
                                  11,
                                  0x54, 0x45, 0x53, 0x54, 0x5F, 0x41, 0x53, 0x53, 0x45, 0x54, 0x32,
                                  0x00, 0xE1, 0xF5, 0x05, 0x00, 0x00, 0x00, 0x00,
                                  2, 0, 0,
                                  0x75
                              ])))

        # Asset reissue w/ no ipfs
        tx.vout.append(CTxOut(nValue=0,
                              scriptPubKey=script_pub_key +
                              bytes([
                                  0xc0, 47,
                                  0x72, 0x76, 0x6E,
                                  0x72,
                                  12,
                                  0x52, 0x45, 0x49, 0x53, 0x53, 0x55, 0x45, 0x5F, 0x49, 0x50, 0x46, 0x53,
                                  0x00, 0x0, 0, 0, 0, 0, 0, 0,
                                  1, 1,
                                  0x75
                              ])))

        # Asset reissue no change w/ ipfs
        tx.vout.append(CTxOut(nValue=0,
                              scriptPubKey=script_pub_key +
                              bytes([
                                  0xc0, 47,
                                  0x72, 0x76, 0x6E,
                                  0x72,
                                  14,
                                  0x52, 0x45, 0x49, 0x53, 0x53, 0x55, 0x45, 0x5F, 0x4E, 0x4F, 0x49, 0x50, 0x46, 0x53,
                                  0x00, 0x0, 0, 0, 0, 0, 0, 0,
                                  0xff, 0,
                                  0x12, 0x20, 0x84, 0x43, 0xbc, 0xbb, 0x6a, 0x01, 0x18, 0xae, 0xbf, 0xcf, 0xe9, 0x1c, 0x12, 0x5d, 0x6e,
                                  0x58, 0xa8, 0x76, 0x93, 0xb7, 0x3d, 0x08, 0xf7, 0x7d, 0x77, 0xf6, 0xe7, 0x8f, 0xa2, 0x29, 0x56, 0x3c,
                                  0x75
                              ])))

        # Null tag script
        tx.vout.append(CTxOut(nValue=0,
                              scriptPubKey=bytes.fromhex('c014b98ce5280197c46eb0c8423534fe81cbdedf9aef110f2353595354454d2f2347414c41585901')))

        # Null verifier script
        tx.vout.append(CTxOut(nValue=0,
                              scriptPubKey=bytes.fromhex('c050141328 51 55 41 4C 31 7C 51 55 41 4C 32 29 26 51 55 41 4C 33')))

        # Null global freeze
        tx.vout.append(CTxOut(nValue=0,
                              scriptPubKey=bytes.fromhex('c050500c0a24424f4e4f5f4d41494e00')))

        '''

        for i in range(len(tx.vin)):
            self.untrusted_hash_tx_input_start(tx=tx,
                                               inputs=inputs,
                                               input_index=i,
                                               script=tx.vin[i].scriptSig,
                                               is_new_transaction=(i == 0))

        self.untrusted_hash_tx_input_finalize(tx=tx,
                                              change_path=change_path)

        sigs: List[Tuple[bytes, bytes, Tuple[int, bytes]]] = []
        for i in range(len(tx.vin)):
            self.untrusted_hash_tx_input_start(tx=tx,
                                               inputs=[inputs[i]],
                                               input_index=0,
                                               script=tx.vin[i].scriptSig,
                                               is_new_transaction=False)
            _, _, amount = utxos[i]
            sigs.append(
                (bip143_digest(tx, amount, i),
                 sign_pub_keys[i],
                 self.untrusted_hash_sign(sign_path=sign_paths[i],
                                          lock_time=tx.nLockTime,
                                          sig_hash=1))
            )

        return sigs

    def sign_tx(self,
                tx: CTransaction,
                change_path: str,
                sign_paths: List[str],
                utxos: List[Tuple[CTransaction, int, int]]) -> List[Tuple[int, bytes]]:
        inputs: List[Tuple[CTransaction, bytes]] = [
            (utxo, self.get_trusted_input(utxo=utxo, output_index=output_index))
            for utxo, output_index, _ in utxos
        ]

        for i in range(len(tx.vin)):
            self.untrusted_hash_tx_input_start(tx=tx,
                                               inputs=inputs,
                                               input_index=i,
                                               script=tx.vin[i].scriptSig,
                                               is_new_transaction=(i == 0))

        self.untrusted_hash_tx_input_finalize(tx=tx,
                                              change_path=change_path)

        sigs = []
        for i in range(len(tx.vin)):
            self.untrusted_hash_tx_input_start(tx=tx,
                                               inputs=[inputs[i]],
                                               input_index=0,
                                               script=tx.vin[i].scriptSig,
                                               is_new_transaction=False)
            sigs.append(self.untrusted_hash_sign(sign_path=sign_paths[i],
                                                 lock_time=tx.nLockTime,
                                                 sig_hash=1))

        return sigs
