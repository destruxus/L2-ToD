# generate_key.py
from cryptography.fernet import Fernet

# This generates a new, URL-safe base64-encoded key
key = Fernet.generate_key()

print("--- Your Master Encryption Key ---")
print("Copy this entire line and add it to your .env file:")
print(key.decode())