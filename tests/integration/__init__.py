"""Integration tests for the MCP call-orchestrator proxy.

These tests require a live backend MCP server described by
tests/integration/backend.json (copy backend.example.json and fill in).
They exercise the proxy's guarantees (parity, serialization, multi-session
isolation, resilience) against whatever real MCP backend the user configures.
"""
