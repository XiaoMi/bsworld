# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0

"""Summarize protocol verdicts for component-wide and qualification-only runs."""


def pytest_terminal_summary(terminalreporter):
    stats = terminalreporter.stats
    errors = {report.nodeid for report in stats.get('error', ())}
    verdicts = [
        value
        for report in stats.get('passed', ())
        if report.nodeid not in errors
        for name, value in report.user_properties
        if name == 'protocol_verdict'
    ]
    if verdicts:
        terminalreporter.write_line(
            f'Protocol verdicts: {verdicts.count("PASS")} PASS, '
            f'{verdicts.count("PARTIAL")} PARTIAL'
        )
