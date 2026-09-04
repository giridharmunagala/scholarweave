"""Persisted agent blueprints and the native harness that runs them.

Submodules are imported directly (``backend.agents.harness``,
``backend.agents.compiler``) so provider and runtime modules can depend on the
harness without an import cycle through this package.
"""
