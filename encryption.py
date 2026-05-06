from cryptography.fernet import Fernet
from pathlib import Path
import os
import base64

# In production, set ENCRYPTION_KEY in the environment and keep it secret.[web:92][web:124]
# In local/dev, fall back to an ephemeral generated key so the beginner workflow
# keeps working. The app-level validator in app.py is what enforces ENCRYPTION_KEY
# in production with a clean, secret-free error message; we only avoid crashing
# here on import so that error path can run.
_raw_key = os.environ.get("ENCRYPTION_KEY")
if _raw_key:
    ENCRYPTION_KEY = _raw_key.encode()
    try:
        cipher = Fernet(ENCRYPTION_KEY)
    except Exception:
        # Malformed key. Defer the error to the app-level validator so the
        # operator sees a single, clean "ENCRYPTION_KEY is not a valid Fernet
        # key" message instead of a stack trace from cryptography internals.
        cipher = None
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