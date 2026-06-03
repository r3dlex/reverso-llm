"""Unit tests for middleware package exports."""


def test_middleware_exports_x_gateway_callback():
    import reverso.middleware as middleware

    assert callable(middleware.x_gateway_callback)


def test_x_gateway_callback_no_longer_infers_minimax_provider():
    from reverso.middleware.x_gateway_callback import _infer_provider

    assert _infer_provider("MiniMax-M3") == "unknown"
    assert _infer_provider("custom_openai/MiniMax-M3") == "unknown"
    assert _infer_provider("minimax") == "unknown"
