import base64
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from coworker.config import get_settings


@dataclass(frozen=True)
class Envelope:
    """A self-describing encrypted blob: version || nonce || wrapped_dek || ciphertext."""
    version: int
    nonce: bytes
    wrapped_dek: bytes
    ciphertext: bytes

    def serialize(self) -> bytes:
        return (
            self.version.to_bytes(1, "big")
            + len(self.nonce).to_bytes(1, "big")
            + self.nonce
            + len(self.wrapped_dek).to_bytes(2, "big")
            + self.wrapped_dek
            + self.ciphertext
        )

    @classmethod
    def deserialize(cls, data: bytes) -> "Envelope":
        version = data[0]
        nonce_len = data[1]
        nonce = data[2:2 + nonce_len]
        offset = 2 + nonce_len
        wrapped_dek_len = int.from_bytes(data[offset:offset + 2], "big")
        offset += 2
        wrapped_dek = data[offset:offset + wrapped_dek_len]
        offset += wrapped_dek_len
        ciphertext = data[offset:]
        return cls(version=version, nonce=nonce, wrapped_dek=wrapped_dek, ciphertext=ciphertext)


class EnvelopeCipher:
    """AES-256-GCM with envelope wrapping.

    The master key is the long-lived KEK (key encryption key).
    Every encrypt() generates a fresh DEK (data encryption key), encrypts
    the payload with it, then encrypts the DEK with the KEK.
    """

    VERSION = 1

    def __init__(self, master_key: bytes):
        if len(master_key) != 32:
            raise ValueError("Master key must be 32 bytes (256 bits)")
        self._kek = AESGCM(master_key)

    def encrypt(self, plaintext: bytes, *, associated_data: bytes | None = None) -> bytes:
        """Encrypt plaintext; return serialized envelope bytes safe to store in DB."""
        dek = AESGCM.generate_key(bit_length=256)
        dek_cipher = AESGCM(dek)

        # Encrypt the actual payload with the DEK
        data_nonce = os.urandom(12)
        ciphertext = dek_cipher.encrypt(data_nonce, plaintext, associated_data)

        # Wrap (encrypt) the DEK with the KEK
        kek_nonce = os.urandom(12)
        wrapped_dek = self._kek.encrypt(kek_nonce, dek, associated_data)
        wrapped_dek = kek_nonce + wrapped_dek  # prepend the nonce

        envelope = Envelope(
            version=self.VERSION,
            nonce=data_nonce,
            wrapped_dek=wrapped_dek,
            ciphertext=ciphertext,
        )
        return envelope.serialize()

    def decrypt(self, blob: bytes, *, associated_data: bytes | None = None) -> bytes:
        envelope = Envelope.deserialize(blob)
        if envelope.version != self.VERSION:
            raise ValueError(f"Unsupported envelope version {envelope.version}")

        # Unwrap the DEK
        kek_nonce = envelope.wrapped_dek[:12]
        wrapped = envelope.wrapped_dek[12:]
        dek = self._kek.decrypt(kek_nonce, wrapped, associated_data)
        dek_cipher = AESGCM(dek)

        # Decrypt the payload
        return dek_cipher.decrypt(envelope.nonce, envelope.ciphertext, associated_data)


_cipher: EnvelopeCipher | None = None


def get_cipher() -> EnvelopeCipher:
    global _cipher
    if _cipher is None:
        settings = get_settings()
        master_key = base64.b64decode(settings.MASTER_ENCRYPTION_KEY.get_secret_value())
        _cipher = EnvelopeCipher(master_key)
    return _cipher


def encrypt_str(value: str, *, firm_id: str | None = None) -> bytes:
    """Encrypt a string with optional firm-id binding (cross-firm decryption fails)."""
    associated_data = firm_id.encode() if firm_id else None
    return get_cipher().encrypt(value.encode("utf-8"), associated_data=associated_data)


def decrypt_str(blob: bytes, *, firm_id: str | None = None) -> str:
    associated_data = firm_id.encode() if firm_id else None
    return get_cipher().decrypt(blob, associated_data=associated_data).decode("utf-8")
