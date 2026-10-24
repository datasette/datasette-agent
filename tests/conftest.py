import pytest


@pytest.fixture
def capture_chain_calls(monkeypatch):
    """Call with the dotted path of a module's datasette-llm LLM to swap
    it for one whose models record each chain() call's kwargs and the
    Prompt of every response the chain produces - the messages a real
    adapter is handed. Returns (chain_calls, prompts)."""
    from datasette_llm import LLM

    def capture(target):
        chain_calls = []
        prompts = []

        class CapturingLLM(LLM):
            async def model(self, *args, **kwargs):
                model = await super().model(*args, **kwargs)
                real_chain = model.chain

                def chain(*chain_args, **chain_kwargs):
                    chain_calls.append(chain_kwargs)
                    chain_response = real_chain(*chain_args, **chain_kwargs)
                    real_responses = chain_response.responses

                    async def responses():
                        async for response in real_responses():
                            prompts.append(response.prompt)
                            yield response

                    chain_response.responses = responses
                    return chain_response

                model.chain = chain
                return model

        monkeypatch.setattr(target, CapturingLLM)
        return chain_calls, prompts

    return capture
