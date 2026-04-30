"""Pytest configuration and shared fixtures for CoWorker v3 tests."""
import pytest


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"
