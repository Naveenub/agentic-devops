from agent import report


def test_report_is_deterministic_and_all_cases_pass():
    a, b = report.render(report.build_report()), report.render(report.build_report())
    assert a == b
    assert report.build_report()["summary"]["failed"] == 0


def test_committed_report_is_current():
    assert report.main(["--check"]) == 0
