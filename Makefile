.PHONY: ingest eval test report report-check

ingest:
	python -m agent.memory.ingest

eval:
	python -m agent.eval
	python -m agent.hallucination_eval

test:
	python -m pytest -q

report:
	python -m agent.report

report-check:
	python -m agent.report --check
