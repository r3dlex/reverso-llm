"""Provider adapter implementations for the first-party Responses gateway.

Each module here implements the frozen reverso.protocols.adapter.ProviderAdapter
Protocol for one provider (claude, copilot) and is injected into the app by
prefix. This package marker stays minimal so independent adapter lanes do not
clobber one another.
"""
