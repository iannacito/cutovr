from cryptography.fernet import Fernet
from pathlib import Path
import os
import base64

# In production, set ENCRYPTION_KEY in the environment and keep it secret.[web:92][web:124]
_raw_key = os.environ.get("ENCRYPTION_KEY")
if _raw_key:
    ENCRYPTION_KEY = _raw_key.encode()
else:
    ENCRYPTION_KEY = Fernet.generate_key()

cipher = Fernet(ENCRYPTION_KEY)

def encrypt_file(input_path, output_path):
    """Encrypt a file to output_path using AES-256 via Fernet."""
    input_path = Path(input_path)
    output_path = Path(output_path)
    data = input_path.read_bytes()
    encrypted = cipher.encrypt(data)
    output_path.write_bytes(encrypted)

def decrypt_file(input_path, output_path):
    """Decrypt a previously encrypted file."""
    input_path = Path(input_path)
    output_path = Path(output_path)
    encrypted = input_path.read_bytes()
    decrypted = cipher.decrypt(encrypted)
    output_path.write_bytes(decrypted)

def encrypt_token(token_string: str) -> str:
    """Encrypt a token string and return base64 text."""
    encrypted = cipher.encrypt(token_string.encode())
    return base64.b64encode(encrypted).decode()

def decrypt_token(encrypted_token: str) -> str:
    """Decrypt a token string that was encrypted_token()."""
    encrypted = base64.b64decode(encrypted_token.encode())
    return cipher.decrypt(encrypted).decode()