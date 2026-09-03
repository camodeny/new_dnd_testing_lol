"""Auth domain — Supabase JWT/JWKS verification and profile resolution.

Modules:
  errors.py  — typed ``AuthError`` (plain Python, no FastAPI)
  jwt.py     — JWT/JWKS verification (no FastAPI)
  service.py — pure profile resolution + mock auth (no FastAPI)
  router.py  — ``/api/me`` and ``/api/auth/config`` (transport)
"""
