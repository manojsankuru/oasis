"""Experiment runners. Nothing here is imported by the agent or by a tool.

Each module in this package produces a table the paper quotes. They read the
shipped modules and never reimplement one: a runner that recomputed a number
would be a second answer for `critic.py` to trace to.

    python -m src.experiments.faults      # the robustness table
    python -m src.experiments.behaviour   # the adversarial suite
    python -m src.experiments.transfer    # the second-county run
"""
