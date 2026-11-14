import os

import pytest


@pytest.fixture(autouse=True)
def _no_model_headwords(request, monkeypatch):
    """Saving a vocab word asks the text model for its headword (webapp/lemma).
    Tests file words under their printed form instead, unless they're about the
    headword logic itself (marked `real_headword`) and stub the model themselves."""
    if request.node.get_closest_marker("real_headword"):
        return
    os.environ.setdefault("GEMINI_API_KEY", "test-key")   # pipeline.gem needs one at import
    import webapp.lemma
    monkeypatch.setattr(webapp.lemma, "headword", lambda form, context=None: form)
