"""Static contract checks for the endpoint contract (#42), hosted per #52.

A package rather than a bare directory so pytest derives module names from here
(`contract.test_x`) instead of from the basename: `tests/` and `src/common/tests/`
already collide on `test_no_process_launch.py`, and the two suites cannot be collected in
one invocation because of it. Keeping this a package means a name added here can never
join that collision.
"""
