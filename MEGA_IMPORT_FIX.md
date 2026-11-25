# MEGA SDK import fix

The Docker build previously installed `mega.py>=1.0.8` with `--no-deps`.
That PyPI distribution can provide the `mega` Python namespace for the ordinary
MEGA API client, which does not expose the SDK binding symbols (`MegaApi`,
`MegaCancelToken`, etc.) expected by LMB. Installing it can shadow the SDK
binding supplied by the base image and trigger the import failure.

The Dockerfile now no longer overlays that namespace with `mega.py`.
Python syntax validation was run across the repository (217 Python files).

This is a targeted packaging fix, not a claim that every runtime path has been
tested against a live Telegram account, MEGA account, or VPS.
