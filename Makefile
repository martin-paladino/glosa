.PHONY: test run demo bench samples

# Fast, free test suite (no live API calls). See pytest.ini: `live` tests are
# excluded by default.
test:
	uv run pytest

# Start the Glosa server. Implemented in T7 (glosa/web/app.py + uvicorn entrypoint).
run:
	@echo "pending T7"

# Bring up 2 rooms with the bundled sample clips, API key only. Implemented in T15.
demo:
	@echo "pending T15"

# Compare the "fast" and "glossary" engines on the same audio: latency,
# quality and glossary-term accuracy. Implemented in T15 (bench/bench.py).
bench:
	@echo "pending T15"

# Download the longer sample clips used by scripts/verify_live.py and bench.
# Implemented in T15.
samples:
	@echo "pending T15"
