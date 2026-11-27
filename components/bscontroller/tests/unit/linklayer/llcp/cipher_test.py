# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""AES-CCM packet cipher vectors and nonce limits (Core Vol 6, Part E, 1-2).

Pure functions, independent of any procedure: published sample data, header
masking, per-direction counters and exhaustion. These tests drive
``PacketCipher``/``PacketSecurity`` directly and do not use the LLCP harness.
"""

import pytest
from cryptography.exceptions import InvalidTag

from bscontroller.linklayer import LlId
from bscontroller.linklayer.packet_security import PacketCipher, PacketSecurity
from tests.unit.linklayer.llcp.harness import ControlOpcode

# Vol 6, Part C, 1: sample session key and IV.
KEY = bytes.fromhex('99AD1B5226A37E3E058E3B8E27C2C666')
IV = bytes.fromhex('24ABDCBABEBAAFDE')
COUNTER_LIMIT = 1 << 39


@pytest.mark.parametrize(
    'central,expected', [(True, '9FCDA7F448'), (False, 'A34C13A415')]
)
def test_start_encryption_sample_vector(central, expected):
    """Encrypting LL_START_ENC_RSP matches the Core sample vector.

    Purpose: Verify that PacketCipher reproduces the published encryption of the
    first control PDU for each direction, confirming key, IV and nonce layout
    (Vol 6, Part C, 1).

    Steps:
        1. Build a PacketCipher from the sample key and IV, for each direction.
        2. Encrypt the single-octet LL_START_ENC_RSP control PDU.

    Expected:
        - The ciphertext plus MIC equals the direction's published vector, for
          both central=True and central=False.
    """
    cipher = PacketCipher(KEY, IV, central=central)
    assert cipher.encrypt(
        LlId.CONTROL, bytes([ControlOpcode.LL_START_ENC_RSP])
    ) == bytes.fromhex(expected)


def test_header_nesn_sn_md_are_masked_and_duplicate_reuses_counter():
    """NESN/SN/MD are excluded from the MIC and a retransmission reuses the counter.

    Purpose: Verify that only the LLID bits of the header authenticate the PDU,
    so changing ARQ bits does not break decryption, while a flipped LLID does;
    and that a duplicate decrypt does not advance the receive counter.

    Steps:
        1. Encrypt a payload with the central cipher.
        2. Decrypt it with the peer cipher (duplicate=False).
        3. Decrypt again with NESN/SN/MD bits set and duplicate=True.
        4. Decrypt once more with a different LLID.

    Expected:
        - Both decrypts recover the plaintext despite the changed ARQ bits.
        - After the duplicate, rx_counter and tx_counter are both 1.
        - Decrypting with a changed LLID raises InvalidTag.
    """
    tx = PacketCipher(KEY, IV, central=True)
    rx = PacketCipher(KEY, IV, central=False)
    wire = tx.encrypt(LlId.START, b'encrypted payload')
    assert rx.decrypt(LlId.START, wire, duplicate=False) == b'encrypted payload'
    assert rx.decrypt(LlId.START | 0x1C, wire, duplicate=True) == b'encrypted payload'
    assert rx.rx_counter == tx.tx_counter == 1
    # LLID is authenticated.
    with pytest.raises(InvalidTag):
        rx.decrypt(LlId.CONTINUATION, wire, duplicate=True)


def test_direction_bit_separates_nonces():
    """The nonce direction bit prevents a same-direction receiver from decrypting.

    Purpose: Verify that the per-direction bit in the nonce separates the two
    traffic directions, so a cipher configured for the sender's direction cannot
    authenticate what the sender produced.

    Steps:
        1. Encrypt a payload with a central (sender) cipher.
        2. Decrypt it with a second cipher also configured as central.

    Expected:
        - The same-direction decrypt raises InvalidTag.
    """
    central = PacketCipher(KEY, IV, central=True)
    wire = central.encrypt(LlId.START, b'x')
    # A receiver expecting the same direction cannot authenticate it.
    with pytest.raises(InvalidTag):
        PacketCipher(KEY, IV, central=True).decrypt(LlId.START, wire, duplicate=False)


def test_empty_pdus_have_no_mic_or_counter():
    """An empty PDU is passed through without a MIC and without counting.

    Purpose: Verify that zero-length payloads are neither encrypted nor
    authenticated and never consume a nonce counter.

    Steps:
        1. Encrypt an empty continuation payload.
        2. Decrypt an empty continuation payload.

    Expected:
        - Both operations return an empty byte string.
        - tx_counter and rx_counter remain 0.
    """
    cipher = PacketCipher(KEY, IV, central=True)
    assert cipher.encrypt(LlId.CONTINUATION, b'') == b''
    assert cipher.decrypt(LlId.CONTINUATION, b'', duplicate=False) == b''
    assert cipher.tx_counter == cipher.rx_counter == 0


@pytest.mark.parametrize('size', [1, 4])
def test_payload_without_room_for_mic_is_invalid(size):
    """A ciphertext too short to hold a MIC is rejected.

    Purpose: Verify that decrypting a non-empty payload smaller than the 4-octet
    MIC fails rather than reading past the buffer, for a short and a mid-size
    input.

    Steps:
        1. Decrypt a payload of ``size`` zero bytes (1 and 4).

    Expected:
        - Decryption raises InvalidTag whose message mentions 'payload and MIC'.
    """
    with pytest.raises(InvalidTag, match='payload and MIC'):
        PacketCipher(KEY, IV, central=False).decrypt(
            LlId.START, bytes(size), duplicate=False
        )


def test_counter_exhaustion_never_reuses_a_nonce():
    """The last nonce is usable, then further traffic raises OverflowError.

    Purpose: Verify that the 39-bit packet counter is honoured at its boundary:
    the final counter value works once (and for its retransmission), but moving
    past it is refused rather than reusing a nonce.

    Steps:
        1. Set both ciphers to the last valid counter.
        2. Encrypt and decrypt a PDU at that counter (duplicate=False then True).
        3. Attempt one more encrypt and one more fresh decrypt.

    Expected:
        - The boundary PDU round-trips, and its duplicate also decrypts.
        - A further encrypt raises OverflowError.
        - A further non-duplicate decrypt raises OverflowError.
    """
    sender = PacketCipher(KEY, IV, central=True)
    receiver = PacketCipher(KEY, IV, central=False)
    sender.tx_counter = receiver.rx_counter = COUNTER_LIMIT - 1
    wire = sender.encrypt(LlId.START, b'last')
    assert receiver.decrypt(LlId.START, wire, duplicate=False) == b'last'
    assert receiver.decrypt(LlId.START, wire, duplicate=True) == b'last'
    with pytest.raises(OverflowError):
        sender.encrypt(LlId.START, b'overflow')
    with pytest.raises(OverflowError):
        receiver.decrypt(LlId.START, wire, duplicate=False)


@pytest.mark.parametrize('counter', [-1, COUNTER_LIMIT])
def test_nonce_rejects_out_of_range_counter(counter):
    """Building a nonce outside the 39-bit range is refused.

    Purpose: Verify that the nonce constructor validates the counter, rejecting a
    negative value and the first value past the limit.

    Steps:
        1. Call nonce(counter, True) with counter -1 and with COUNTER_LIMIT.

    Expected:
        - Each call raises OverflowError.
    """
    with pytest.raises(OverflowError):
        PacketCipher(KEY, IV, central=True).nonce(counter, True)


@pytest.mark.parametrize('key,iv', [(bytes(24), IV), (KEY, bytes(7))])
def test_cipher_rejects_non_le_key_or_iv(key, iv):
    """A wrong-sized key or IV is rejected at construction.

    Purpose: Verify that PacketCipher enforces a 128-bit key and 64-bit IV,
    rejecting an over-long key and a too-short IV.

    Steps:
        1. Construct PacketCipher with a 24-byte key, then with a 7-byte IV.

    Expected:
        - Each construction raises ValueError whose message mentions '128-bit
          key'.
    """
    with pytest.raises(ValueError, match='128-bit key'):
        PacketCipher(key, iv, central=True)


def test_packet_security_encrypts_only_when_enabled():
    """PacketSecurity passes plaintext through until transmit encryption is on.

    Purpose: Verify that PacketSecurity gates the cipher: output is unchanged
    while disabled, and becomes ciphertext (advancing the counter) once
    tx_enabled is set.

    Steps:
        1. Encrypt a payload with tx_enabled still false.
        2. Set tx_enabled and encrypt the same payload again.

    Expected:
        - The first result equals the plaintext.
        - The second result differs from the plaintext.
        - The cipher's tx_counter is 1 afterwards.
    """
    security = PacketSecurity(PacketCipher(KEY, IV, central=True))
    assert security.encrypt(LlId.START, b'plain') == b'plain'
    security.tx_enabled = True
    assert security.encrypt(LlId.START, b'plain') != b'plain'
    assert security.cipher.tx_counter == 1
