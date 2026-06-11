"""Package data shipped with the protocols namespace.

Holds JSON artifacts that the protocols runtime reads via importlib.resources
so they remain available when the wheel is installed outside the source tree.
The single file today is responses_parity_surface.json, a mirror of
.omc/research/responses-parity-surface.json (the human-authored source of
truth). A unit test asserts the two stay byte-identical so the table cannot
drift from the research artifact.
"""
