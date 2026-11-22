.PHONY: ingest eval live-eval test report report-check

ingest:
	python -m agent.memory.ingest

eval:
	python -m agent.eval
	python -m agent.hallucination_eval

test:
	python -m pytest -q

live-eval:
	python -m agent.eval --live
	python -m agent.hallucination_eval --judge

report:
	python -m agent.report

report-check:
	python -m agent.report --check
