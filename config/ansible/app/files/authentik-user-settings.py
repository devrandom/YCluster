# Custom Django settings, imported by authentik via its
# /data/user_settings.py hook (mounted by the compose file).
#
# The stock hasher list plus plain BCryptPasswordHasher, which verifies the
# bcrypt hashes imported from Open-WebUI ('ycluster user import-owui') —
# the stock BCryptSHA256 variant hashes sha256(password) and cannot.
# Django re-hashes to the first (preferred) hasher on each user's next
# successful login, so imported hashes converge away; keep this file until
# authentik_core_user has no 'bcrypt$' passwords left.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
    "django.contrib.auth.hashers.BCryptPasswordHasher",
]
