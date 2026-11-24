"""Client preference learning prototype for synthetic portfolio workflows."""
from .context import build_context
from .learner import load_rules, rebuild_state, update_state
from .runtime import PortfolioLearner
from .signals import extract_signals

__all__ = ["PortfolioLearner", "build_context", "extract_signals", "load_rules", "rebuild_state", "update_state"]
