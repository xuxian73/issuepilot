class StepError(Exception):
    """A step failed. The orchestrator retries once, then hands it to a human."""
