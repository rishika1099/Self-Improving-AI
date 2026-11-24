"""Connect the delivered learner to Portfolio A's observation journal."""

from self_improving_ai.config import settings

from .parser import GeminiParser
from .runtime import PortfolioLearner


class PortfolioParser:
    def parse_scoped(self, text, scope):
        from self_improving_ai.agents.portfolio.model_settings import portfolio_api_key

        parser = GeminiParser(settings.portfolio_model, api_key=portfolio_api_key())
        return parser.parse_scoped(text, scope)


class PortfolioLearningAdapter:
    def __init__(self, learner):
        self.learner = learner

    def process_event(self, event):
        return self.learner.process_event(event)

    def retry_event(self, event):
        return self.learner.process_event(event, retry_pending=True)

    def get_context(self, client_id, scope):
        return self.learner.get_context(client_id, scope)

    def retract_event(self, client_id, event_id):
        # The journal verified this ID even if its input never reached the learner.
        return self.learner.retract_event(client_id, event_id, allow_missing=True)


def create_adapter(*, workspace_dir):
    return PortfolioLearningAdapter(PortfolioLearner(workspace_dir, parser=PortfolioParser()))
