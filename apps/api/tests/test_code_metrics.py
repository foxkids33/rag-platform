from app.evaluation.metrics import exact_code_metrics


def test_command_comparison_is_case_and_whitespace_sensitive():
    expected = ('curl -I https://example.test \\\n  --header "X:  value"',)
    correct = '```bash\n' + expected[0] + '\n```'
    assert exact_code_metrics(expected, correct) == (1.0, True)
    assert exact_code_metrics(expected, correct.replace('-I', '-i')) == (0.0, False)
    assert exact_code_metrics(expected, correct.replace('  --header', '--header')) == (0.0, False)
    assert exact_code_metrics(expected, correct.replace('\n', '\r\n')) == (1.0, True)


def test_code_order_and_absence_are_measured_separately():
    assert exact_code_metrics(('stop service', 'start service'), '```\nstart service\nstop service\n```') == (1.0, False)
    assert exact_code_metrics(('stop service', 'start service'), '```\nstop service\n```') == (0.5, False)
    assert exact_code_metrics((), 'answer') == (None, None)
