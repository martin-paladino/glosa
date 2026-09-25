.PHONY: test run demo demo-fake bench load samples

# Fast, free test suite (no live API calls). See pytest.ini: `live` tests are
# excluded by default.
test:
	uv run pytest

# Start the Glosa server: reads .env (secrets) and config.yaml (your event)
# from the repo root. See README.md for the 3-command setup.
run:
	uv run python -m glosa.web.app

# Bring up 2 rooms with the bundled sample clips (samples/en_clip.opus,
# samples/es_clip.opus) at real time, with the real Gemini engine. Needs
# only GEMINI_API_KEY (and ADMIN_PASSWORD, required to start at all) in
# .env; costs ~$0.10 for the two ~90s clips. Uses the versioned
# config.demo.yaml, never your own config.yaml.
demo:
	GLOSA_CONFIG=config.demo.yaml uv run python -m glosa.web.app

# Same 2 rooms with engine_mode: fake (a recorded session replayed instead
# of the real API): no GEMINI_API_KEY spent, good for trying Glosa or
# smoke-testing Docker.
demo-fake:
	GLOSA_CONFIG=config.demo-fake.yaml uv run python -m glosa.web.app

# Compare the "fast" and "glossary" engines on the same audio: latency,
# quality and glossary-term accuracy. Implemented in T15 (bench/bench.py).
bench:
	@echo "pending T15"

# Simulated fan-out load test (Task 15a, plan-scale): boots a real server
# (python -m glosa.web.app, engine_mode fake -- no API spend) with 50 `file`
# rooms and hits it with 500 concurrent SSE clients for 60s. Measures
# losses, fan-out delay (p50/p95/p99) and server CPU/RSS; appends one row to
# bench/load-results.md. `uv run python bench/load_test.py --help` for the
# other scales run for Task 15a (see bench/load-results.md).
load:
	uv run python bench/load_test.py --rooms 50 --clients 500 --seconds 60

# Download the longer sample clips (the full source talks the short demo
# clips are cut from) to samples/long/ (gitignored), used by
# scripts/verify_live.py and bench. URLs and provenance: samples/README.md.
samples:
	mkdir -p samples/long
	uv run yt-dlp -f bestaudio -x --audio-format opus \
		-o "samples/long/en_talk.%(ext)s" "https://www.youtube.com/watch?v=wyGMy5ic7PE"
	uv run yt-dlp -f bestaudio -x --audio-format opus \
		-o "samples/long/es_talk.%(ext)s" "https://www.youtube.com/watch?v=VJCrOw2uxbk"
