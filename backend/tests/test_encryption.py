import pytest
from coworker.security.encryption import encrypt_str, decrypt_str

def test_encryption_roundtrip():
    plaintext = "test_secret_value"
    ciphertext = encrypt_str(plaintext)
    decrypted = decrypt_str(ciphertext)
    assert decrypted == plaintext

def test_encryption_cross_firm_binding():
    plaintext = "test_secret_value"
    firm_a = "firm_a_id"
    firm_b = "firm_b_id"
    
    ciphertext = encrypt_str(plaintext, firm_id=firm_a)
    
    # Decrypting with correct firm_id should work
    decrypted = decrypt_str(ciphertext, firm_id=firm_a)
    assert decrypted == plaintext
    
    # Decrypting with incorrect firm_id should fail
    with pytest.raises(Exception):
        decrypt_str(ciphertext, firm_id=firm_b)
