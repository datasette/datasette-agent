To run a development localhost server:

    uv run datasette -s plugins.datasette-llm.default_model gpt-6-luna \
      --internal internal.db --create demo.db --root --secret 1 -p 8518 --reload
